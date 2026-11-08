"""One grammar decides what a card or account number is (sync.card_number).

Every door that stores a typed or filed name cuts it with this module, so
the cases here are the product's whole answer to "is this a number": the
two card layouts under any of the separators a slug folds, unbroken runs
of eight, and nothing that is a date, a year range, a phone number or a
short trailing group. The cut must reach its fixed point, or a name cut
once is cut again by the next migration and its id stops matching."""
import random
import time
import unittest

from oikonome.sync import base, card_number

from .card_number_fixture import CASES


class TheSharedCases(unittest.TestCase):
    def test_every_case_cuts_as_written(self):
        for text, want in CASES:
            self.assertEqual(card_number.cut(text), want, text[:60])

    def test_holds_number_is_whether_the_cut_changes_anything(self):
        for text, want in CASES:
            self.assertEqual(card_number.holds_number(text), want != text,
                             text[:60])

    def test_the_cut_is_its_own_fixed_point(self):
        for text, want in CASES:
            self.assertEqual(card_number.cut(want), want, text[:60])

    def test_find_names_what_the_cut_takes_and_mask_its_last_four(self):
        for text, want in CASES:
            nums = card_number.find(text)
            self.assertEqual(bool(nums), want != text, text[:60])
            for n in nums:
                self.assertIn(n, text)
            if nums:
                self.assertEqual(card_number.mask(text),
                                 card_number.last4(nums[-1]))
                self.assertTrue(want.rstrip().endswith(
                    card_number.mask(text)) or "…" + card_number.mask(text)
                    in want, text[:60])
            else:
                self.assertIsNone(card_number.mask(text))
        self.assertEqual(card_number.find("Visa 4000.1234.5678.9010 x"),
                         ["4000.1234.5678.9010"])
        self.assertEqual(card_number.last4("4000-0000-0000-0002"), "0002")

    def test_a_name_store_and_its_old_spellings_are_one_function(self):
        self.assertIs(base.mask_account_name, card_number.cut)
        self.assertIs(base.mask_long_digits, card_number.cut_for_hash)
        self.assertEqual(base.NUMBERED_NAME_PATTERN, card_number.sql_selector())

    def test_non_text_passes_through(self):
        for v in (None, 12, b"4000 0000 0000 0002"):
            self.assertIs(card_number.cut(v), v)
            self.assertIs(card_number.cut_for_hash(v), v)
            self.assertFalse(card_number.holds_number(v))


class RandomText(unittest.TestCase):
    """Text built from digits and every separator, seeded so a failure
    replays: the cut is idempotent on all of it, and the selector picks up
    everything the cut changes."""

    def test_idempotent_and_selected(self):
        import re
        sel = re.compile(card_number.sql_selector())
        rnd = random.Random(20261106)
        alphabet = "0123456789" * 4 + " ./-…a"
        for _ in range(20000):
            s = "".join(rnd.choice(alphabet)
                        for _ in range(rnd.randint(1, 48)))
            c = card_number.cut(s)
            self.assertEqual(card_number.cut(c), c, repr(s))
            if c != s:
                self.assertTrue(sel.search(s), repr(s))


class TheHashInputNeverMoves(unittest.TestCase):
    def test_cut_for_hash_is_the_unbroken_run_cut_alone(self):
        import re
        rnd = random.Random(7)
        for text, _ in CASES:
            self.assertEqual(card_number.cut_for_hash(text), re.sub(
                r"\d{8,}", lambda m: "…" + m.group()[-4:], text))
        for _ in range(2000):
            s = "".join(rnd.choice("0123456789 -.") for _ in range(30))
            self.assertEqual(card_number.cut_for_hash(s), re.sub(
                r"\d{8,}", lambda m: "…" + m.group()[-4:], s))


class Ends(unittest.TestCase):
    def test_ends_in_number(self):
        for text in ("visa-4000-0000-0000-0002", "mc-5500000000000004",
                     "amex-3400-000000-00009", "x 4000.0000.0000.0002"):
            self.assertTrue(card_number.ends_in_number(text), text)
        for text in ("visa-0002", "fy-2023-2024", "visa-4000-0000-0000-0002-2",
                     ""):
            self.assertFalse(card_number.ends_in_number(text), text)


class Slugs(unittest.TestCase):
    def test_a_slug_is_cut_until_it_spells_no_number(self):
        self.assertEqual(card_number.cut_slug("visa-4000-0000-0000-0002"),
                         "visa-0002")
        # the cut's last four join the digits after them once slugged
        self.assertEqual(card_number.cut_slug("x-1234-1234-1234-12345678"),
                         "x-5678")
        self.assertEqual(card_number.cut_slug("checking"), "checking")
        self.assertEqual(card_number.cut_slug("a" * 50 + "-12345678",
                                              width=40), "a" * 40)

    def test_a_name_is_read_as_its_slug_reads_it(self):
        self.assertEqual(card_number.cut_as_slug_reads(
            "Visa_4000_0000_0000_0002"), "Visa_…0002")
        self.assertEqual(card_number.cut_as_slug_reads("Visa, 4000 , 0000"),
                         "Visa, 4000 , 0000")


class LinearTime(unittest.TestCase):
    def test_long_cells_are_cut_quickly(self):
        for cell in ("1" * 100_000, "1234 " * 20_000, "1234-1234 " * 10_000,
                     "1234 1234 1234 12345 " * 5_000, "…1234 " * 20_000,
                     "12345678 1234 1234 1234 " * 4_000):
            t0 = time.monotonic()
            card_number.cut(cell)
            card_number.find(cell)
            self.assertLess(time.monotonic() - t0, 1.0, cell[:30])


if __name__ == "__main__":
    unittest.main()
