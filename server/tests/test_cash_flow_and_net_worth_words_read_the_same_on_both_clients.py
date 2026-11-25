"""Cash Flow's and Net Worth's small words, the same on web and phone.

- A change that rounds to $0 reads "unchanged", never "+$0" (the Net
  Worth headline and tiles, Cash Flow's best month): `changeWords`.
- "Saved, by month" draws the month in progress once — its saved-so-far
  bar, not a forecast bar beside it.
- Best and worst months carry their year, and a year label reads
  "Jan '06", not "Jan 06'": `monthWithYear`.
- No figure is printed from a client fallback: no "$0 mortgage payoff"
  when the server sends none, no `ytd ? ytd.net : 0`, no `?? 0` on a
  figure the server always sends.
- The footer says when the figures were built, not that they were cached
  (they are built on each request)."""

import pathlib
import unittest

APP = pathlib.Path(__file__).resolve().parents[2]
WEB = APP / "webapp" / "src"
MOB = APP / "mobile" / "src"


def _src(p: pathlib.Path) -> str:
    return p.read_text()


class BothClientsSayItTheSameWay(unittest.TestCase):
    def test_a_zero_change_reads_unchanged(self):
        for p in (WEB / "pages" / "NetWorth.tsx", WEB / "pages" / "CashFlow.tsx",
                  MOB / "app" / "networth.tsx", MOB / "app" / "cashflow.tsx"):
            self.assertIn("changeWords(", _src(p), p.name)
        self.assertIn('return { text: "unchanged", tone: "mut" }',
                      _src(WEB / "moneymath.ts"))
        self.assertIn('return { text: "unchanged", tone: "mut" }',
                      _src(MOB / "lib" / "pure.ts"))

    def test_the_month_in_progress_is_drawn_once(self):
        for p in (WEB / "pages" / "CashFlow.tsx", MOB / "app" / "cashflow.tsx"):
            self.assertIn("p[0] !== graph.current[0]", _src(p), p.name)

    def test_months_carry_their_year(self):
        for p in (WEB / "pages" / "CashFlow.tsx", MOB / "app" / "cashflow.tsx"):
            src = _src(p)
            self.assertNotIn("key.slice(2, 4)}'`", src, p.name)
            self.assertIn("monthWithYear(best[0])", src, p.name)
            self.assertIn("monthWithYear(worst[0])", src, p.name)

    def test_no_figure_comes_from_a_client_fallback(self):
        nw = _src(WEB / "pages" / "NetWorth.tsx")
        self.assertNotIn("mortgage_payoff ?? 0", nw)
        self.assertNotIn("invested ?? 0", nw)
        self.assertNotIn("const net = ytd ? ytd.net : 0;",
                         _src(WEB / "pages" / "CashFlow.tsx"))
        phone = _src(MOB / "app" / "networth.tsx")
        for chain in ("invested ?? 0", "cash_in ?? 0", "cash_out ?? 0"):
            self.assertNotIn(chain, phone)
        self.assertNotIn("sec?.shown?.total ?? sec?.total",
                         _src(MOB / "app" / "(tabs)" / "accounts.tsx"))

    def test_the_phone_words_no_caption_the_server_did_not_send(self):
        """What a window is compared against, a card's due line and this
        year's cost are the server's words and figures, or nothing: the
        phone builds none of them from the range or a bare date."""
        cf = _src(MOB / "app" / "cashflow.tsx")
        sp = _src(MOB / "app" / "spending.tsx")
        acc = _src(MOB / "app" / "(tabs)" / "accounts.tsx")
        for src in (cf, sp):
            self.assertNotIn("vs the ${", src)
        self.assertIn('w.prev_text ?? ""', cf)
        self.assertNotIn("a.card_due.slice(5, 7)", acc)
        # the web's gate: the line needs its date as well as its words
        self.assertIn("{a.card_due && a.card_due_text ? (", acc)
        self.assertNotIn("(ytd ? ytd.net : 0)", cf)

    def test_a_refund_year_reads_refunded(self):
        for p in (WEB / "pages" / "CashFlow.tsx", MOB / "app" / "cashflow.tsx"):
            self.assertIn("refunded $", _src(p).replace("${money(-net)}", "$")
                          .replace("${m$(-net)}", "$"), p.name)

    def test_the_footer_does_not_claim_a_cache(self):
        for p in (WEB / "pages" / "NetWorth.tsx", MOB / "app" / "networth.tsx"):
            self.assertNotIn("Reports cached", _src(p), p.name)


if __name__ == "__main__":
    unittest.main()
