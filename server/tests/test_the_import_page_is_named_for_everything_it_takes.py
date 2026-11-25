"""The web Import page takes statements, tax forms and a whole-household
restore, so its heading is "Import" — the name the menu gives it — and not
"Import transactions", which named one of the three.
"""
import pathlib
import unittest

_WEB = pathlib.Path(__file__).resolve().parents[2] / "webapp" / "src"


class ImportPageHeading(unittest.TestCase):
    def test_no_heading_says_transactions_alone(self):
        for rel in ("pages/Import.tsx", "pages/Accounts.tsx"):
            src = (_WEB / rel).read_text()
            self.assertFalse('"Import transactions"' in src, rel)
            self.assertFalse("<h1>Import transactions</h1>" in src, rel)
        self.assertTrue('heading = "Import",'
                        in (_WEB / "pages/Import.tsx").read_text())
        # the menu's own name for the page
        self.assertTrue('"/import": "Import"' in (_WEB / "App.tsx").read_text())


if __name__ == "__main__":
    unittest.main()
