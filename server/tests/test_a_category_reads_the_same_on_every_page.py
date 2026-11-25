"""A category reads one way wherever it is printed.

The bank's keys are upper-case with underscores, and each surface used to
open the underscores and stop, so the ledger pages shouted FOOD AND DRINK
beside a Budget page in Title Case. The invariant: the display form is
Title Case for a bank key, a name somebody typed is left exactly as typed,
and the two clients carry the same rule as the server."""
import pathlib
import unittest

from oikonome.engine.categories import display_label

APP = pathlib.Path(__file__).resolve().parents[2]


class DisplayLabelTests(unittest.TestCase):
    def test_a_bank_key_reads_as_a_title(self):
        self.assertEqual(display_label("FOOD_AND_DRINK"), "Food and Drink")
        self.assertEqual(display_label("FOOD AND DRINK"), "Food and Drink")
        self.assertEqual(display_label("GENERAL_MERCHANDISE"),
                         "General Merchandise")
        self.assertEqual(display_label("RENT_AND_UTILITIES"),
                         "Rent and Utilities")

    def test_a_joining_word_at_the_end_is_a_word_like_any_other(self):
        # "Transfer In" sits beside "Transfer Out" in every list
        self.assertEqual(display_label("TRANSFER_IN"), "Transfer In")
        self.assertEqual(display_label("TRANSFER_OUT"), "Transfer Out")

    def test_a_typed_name_is_left_as_typed(self):
        for name in ("Pet care", "iTunes", "Kids - school", "mortgage"):
            self.assertEqual(display_label(name), name)

    def test_no_category_reads_as_that(self):
        for none in (None, "", "?"):
            self.assertEqual(display_label(none), "Uncategorized")

    def test_the_clients_carry_the_same_rule(self):
        for rel in ("webapp/src/api/client.ts", "mobile/src/lib/pure.ts"):
            src = (APP / rel).read_text()
            self.assertIn("CAT_SMALL_WORDS", src, rel)
            for word in ("and", "or", "of", "the", "to", "in", "for"):
                self.assertIn(f'"{word}"', src.split("CAT_SMALL_WORDS")[1],
                              rel)

    def test_the_emails_print_the_display_form(self):
        tpl = APP / "server/oikonome/web/templates"
        self.assertIn("r.category|cat", (tpl / "today_body.html").read_text())
        self.assertIn("c.label|cat", (tpl / "report_body.html").read_text())


if __name__ == "__main__":
    unittest.main()
