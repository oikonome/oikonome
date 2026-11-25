"""Every over-time toggle opens on the site-wide default window, 1y.

Net Worth opened on All, which drew the whole history (and its earliest,
thinnest years) where Cash Flow opens on 1y. Both clients now take the
window from one constant each — the web's DEFAULT_RANGE, the phone's
DEFAULT_TREND_RANGE — and both Net Worth screens and both Cash Flow
screens start on it. The chosen option is scrolled into view on a narrow
screen, where the track scrolls sideways."""

import pathlib
import re
import unittest

APP = pathlib.Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (APP / rel).read_text()


class OverTimeTogglesOpenOnOneYear(unittest.TestCase):
    def test_the_default_window_is_one_year_on_both_clients(self):
        self.assertRegex(_src("webapp/src/components/RangePicker.tsx"),
                         r'export const DEFAULT_RANGE: Range = "1y";')
        self.assertRegex(_src("mobile/src/lib/pure.ts"),
                         r'export const DEFAULT_TREND_RANGE: TrendRange = "1y";')

    def test_net_worth_and_cash_flow_start_on_it(self):
        for rel, const in (("webapp/src/pages/NetWorth.tsx", "DEFAULT_RANGE"),
                           ("mobile/src/app/networth.tsx",
                            "DEFAULT_TREND_RANGE"),
                           ("mobile/src/app/cashflow.tsx",
                            "DEFAULT_TREND_RANGE")):
            self.assertIn(f"useState<TrendRange>({const})", _src(rel), rel)
        self.assertIn('sp.get("range") : DEFAULT_RANGE)',
                      _src("webapp/src/pages/CashFlow.tsx"))

    def test_the_chosen_option_is_kept_in_view(self):
        web = _src("webapp/src/components/SegmentedControl.tsx")
        self.assertRegex(web, r'querySelector<HTMLElement>\("button\.on"\)')
        self.assertIn("scrollLeft", web)
        phone = _src("mobile/src/components/segmented-control.tsx")
        self.assertTrue(re.search(r"scrollTo\(\{", phone))


if __name__ == "__main__":
    unittest.main()
