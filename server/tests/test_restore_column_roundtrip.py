"""What restore reads back is decided by its column lists, not the export.

The export is SELECT *, so the ZIP carries every column; restore's fixed
column lists decide what actually comes back. A column added to a table
but not to restore silently vanishes on disaster recovery — and a column
deliberately REMOVED from the schema must never be read back in from an
old backup that still carries it.
"""

import pathlib
import unittest

import oikonome
from oikonome.sync import restore


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent / rel).read_text()


class RestoreRoundTripTests(unittest.TestCase):

    def test_the_tax_reserve_nomination_survives(self):
        src = _src("sync/restore.py")
        i = src.index("INSERT INTO business_entity")
        self.assertIn("tax_reserve_account_id", src[i:i + 700])

    def test_the_plaintext_ein_is_never_restored(self):
        """The schema keeps only the last four. An older backup can carry
        a full EIN column, and reading it back would store it again."""
        src = _src("sync/restore.py")
        i = src.index("INSERT INTO income_documents")
        stmt = src[i:i + 400]
        self.assertIn("ein_last4", stmt)
        self.assertNotIn("       ein,", stmt)

    def test_a_legacy_export_is_reduced_not_rejected(self):
        """An old ZIP still restores — the EIN is cut to its last four at
        the door rather than the row being dropped."""
        self.assertEqual(
            restore._ein_last4_for_restore({"ein": "12-3456789"}), "6789")
        self.assertEqual(
            restore._ein_last4_for_restore({"ein_last4": "4321"}), "4321")
        self.assertIsNone(restore._ein_last4_for_restore({}))
        self.assertIsNone(restore._ein_last4_for_restore({"ein": "12"}))

    def test_ein_last4_prefers_the_modern_column(self):
        self.assertEqual(
            restore._ein_last4_for_restore(
                {"ein_last4": "1111", "ein": "12-3456789"}), "1111")
