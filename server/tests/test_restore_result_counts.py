"""A restore's summary says what it did, not how big the file was.

`already_present` — the tally of rows the tenant already had — must never
be summed with the inserts, or re-restoring an archive that inserted
nothing announces the whole file back. The number a person reads after a
restore has to be the number of rows that landed.
"""

import unittest

from oikonome.web.pages import restore_result


class RestoreResultTests(unittest.TestCase):

    def test_already_present_is_not_counted_as_restored(self):
        r = restore_result({"transactions": 0, "accounts": 0,
                            "already_present": 12345})
        self.assertEqual(r["rows"], 0)
        self.assertEqual(r["imported"], 0)
        self.assertIn("12345 already present", r["source"])

    def test_inserts_are_counted_and_notes_surface_as_warnings(self):
        r = restore_result({"transactions": 8000, "accounts": 12,
                            "already_present": 2,
                            "_notes": ["1 AI backend(s) dropped"]})
        self.assertEqual(r["rows"], 8012)
        self.assertEqual(r["imported"], 8000)
        self.assertEqual(r["warnings"], ["1 AI backend(s) dropped"])
        self.assertNotIn("_notes", r["source"])

    def test_no_notes_means_no_warnings(self):
        self.assertIsNone(restore_result({"transactions": 1})["warnings"])


if __name__ == "__main__":
    unittest.main()
