"""Reading an investment line's description must take time linear in its
length. CPython's regex engine holds the GIL while it backtracks, so a
description that drives a pattern quadratic stalls every request in the
process, for every household on the instance — and a CSV upload can put a
field of hundreds of thousands of characters in front of these patterns."""
import time
import unittest

from oikonome.sync import flowmap


class InvestmentLinePatternsStayLinear(unittest.TestCase):

    BUDGET_S = 1.0

    def _timed(self, fn, arg):
        t0 = time.perf_counter()
        fn(arg)
        return time.perf_counter() - t0

    def test_a_huge_digit_run_after_a_trade_word_is_read_quickly(self):
        for desc in ("SALE " + "1" * 200_000,
                     "PURCHASE " + "1," * 100_000,
                     "SALE " + "1" * 200_000 + " shares"):
            self.assertLess(self._timed(flowmap.investment_flow, desc),
                            self.BUDGET_S)
            self.assertLess(self._timed(flowmap.sale_of_security, desc),
                            self.BUDGET_S)

    def test_the_share_count_pattern_is_linear_even_at_full_length(self):
        # The truncation bounds the input; the pattern itself must not
        # depend on it.
        t0 = time.perf_counter()
        flowmap._STRONG_SECURITY_RE.search("SALE " + "1" * 200_000)
        self.assertLess(time.perf_counter() - t0, self.BUDGET_S)

    def test_share_counts_still_mark_a_trade(self):
        self.assertTrue(flowmap.sale_of_security("SALE 1,250.5 SHARES ACME"))
        self.assertTrue(flowmap.sale_of_security("SALE 10 SH ACME"))
        self.assertEqual(
            flowmap.investment_flow("PURCHASE 3.125 shs ACME"), "transfer")
        self.assertFalse(flowmap.sale_of_security("SALE SAMPLE GOODS CO"))


if __name__ == "__main__":
    unittest.main()
