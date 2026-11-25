"""A page whose read fails keeps its title.

Alerts, Reimbursements and Help returned only the failed line, so the
reader saw "The server is busy — try again" with no word of which page
had failed; Month, Year, Receipts and Merchants kept theirs.
"""
import pathlib
import re
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class FailedReadKeepsTitleTests(unittest.TestCase):
    def test_the_failed_branch_carries_the_page_title(self):
        for page, title in (("Alerts", "Alerts"),
                            ("Reimburse", "Reimbursements"),
                            ("Help", "Help")):
            try:
                src = _src(f"pages/{page}.tsx")
            except FileNotFoundError:
                self.skipTest("webapp/ not present")
            branch = re.search(r"if \(q\.isLoadingError\)\s*(.*?);\n",
                               src, re.S)
            self.assertIsNotNone(branch, page)
            self.assertIn(f"<h1>{title}</h1>", branch.group(1), page)


if __name__ == "__main__":
    unittest.main()
