"""Transactions and Accounts print a figure or a label only from a read
that answered.

A search that failed read "0 results · $0.00 total" and a month "0
transactions"; a phone search that failed kept listing the previous
search's rows under the new term; a receipts read that failed was an empty
receipts list. On Accounts, a connections read that failed turned every
bank-fed account into "Import" (a manual account), and a settings read
that failed dropped the excluded and primary pills without a word. The
figures now come through knownOr (the screen-states twin block), greyed
while they are a kept or a previous filter's answer, and each failed read
says so in its place (specs/screen-states.md).
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


class LedgerFiguresWait(unittest.TestCase):
    def test_the_web_ledger_hero_counts_only_from_an_answer(self):
        src = _src(self, _WEB / "pages" / "Transactions.tsx")
        self.assertNotIn("{rows.length} transaction", src)
        self.assertNotIn("money(-(query.data?.amount_sum ?? 0))", src)
        self.assertNotIn("String(query.error)", src)
        for want in ("const shownTotal = knownOr(query, (d) => d.total);",
                     "const shownCount = knownOr(query, (d) => d.rows.length);",
                     "{shownTotal ?? UNKNOWN_FIGURE}",
                     "const greyed = figureGreyed(query);",
                     '<FailedLine q={query} what="transactions" />',
                     "<StaleLine query={query} />"):
            self.assertIn(want, src)

    def test_a_new_phone_search_never_lists_the_last_ones_rows(self):
        src = _src(self, _MOB / "app" / "(tabs)" / "transactions.tsx")
        self.assertIn("pileFor === filterKey && pile.length ? pile : pageRows",
                      src)
        effect = src[src.index("const [pileFor, setPileFor]"):]
        effect = effect[:effect.index("mergePilePages(")]
        # the previous question's page standing in is not merged
        self.assertIn("|| query.isPlaceholderData) return;", effect)
        self.assertIn("setPileFor(filterKey);", effect)
        self.assertIn('<FailedLine q={query} what="transactions" />', src)
        self.assertIn("figureGreyed(query)", src)

    def test_a_failed_receipts_read_is_not_an_empty_list(self):
        for p, q in ((_WEB / "components" / "ReceiptPanel.tsx", "q"),
                     (_MOB / "app" / "txn.tsx", "receipts")):
            src = _src(self, p)
            self.assertIn(f'<FailedLine q={{{q}}} what="receipts" />', src, p)
            self.assertIn(f"<StaleLine query={{{q}}} />", src, p)

    def test_a_row_with_no_hand_off_is_not_called_missing(self):
        src = _src(self, _MOB / "app" / "txn.tsx")
        self.assertNotIn("Transaction not found.", src)
        self.assertIn('to="/transactions" action="Find it on Transactions"', src)


class AccountsLabelsWait(unittest.TestCase):
    def test_an_unread_connection_is_never_labelled_manual(self):
        web = _src(self, _WEB / "pages" / "Accounts.tsx")
        self.assertIn("const connsKnown = conns.data !== undefined;", web)
        self.assertIn("(script || (!g.conn && connsKnown))", web)
        self.assertIn('<FailedLine q={conns} what="your bank connections" />',
                      web)
        self.assertIn('<FailedLine q={settings} what="which accounts are '
                      'excluded or primary" />', web)
        self.assertNotIn("String(accounts.error)", web)
        mob = _src(self, _MOB / "app" / "(tabs)" / "accounts.tsx")
        self.assertIn('if (!conn) return connsKnown ? "Import" : undefined;', mob)
        self.assertNotIn('if (!conn) return "Import";', mob)
        self.assertIn('<FailedLine q={conns} what="your bank connections" />',
                      mob)
        self.assertIn('<FailedLine q={settings} what="which accounts are '
                      'excluded or primary" />', mob)


if __name__ == "__main__":
    unittest.main()
