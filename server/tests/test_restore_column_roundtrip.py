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
        from oikonome.engine import entities
        from oikonome.sync import export

        from .test_every_table_round_trips_through_its_lander import (
            fresh_household)
        from .util import make_db, write_config
        # an empty destination: one holding its own "chk" keeps it, and a
        # reserve that is not the business's own account is left out
        src, dest = make_db(), fresh_household()
        self.addCleanup(src.close)
        self.addCleanup(dest.close)
        write_config(src)
        eid = str(entities.create_entity(
            src, name="Invented Works LLC", structure="sole_prop",
            business_start_date="2030-01-01")["id"])
        entities.assign_account(src, "chk", eid)
        entities.update_entity(src, eid, tax_reserve_account_id="chk")
        restore.restore_zip(dest, export.build_zip(src))
        got = dest.execute("SELECT tax_reserve_account_id AS a FROM"
                           " business_entity WHERE id = %s", (eid,)).fetchone()
        self.assertEqual(got["a"], "chk")

    def test_whose_business_it_is_survives_and_a_stray_value_is_unknown(self):
        from oikonome.engine import entities
        from oikonome.sync import landers

        from .test_every_table_round_trips_through_its_lander import (
            fresh_household)
        from oikonome.sync import export
        from .util import make_db, write_config
        src, dest = make_db(), fresh_household()
        self.addCleanup(src.close)
        self.addCleanup(dest.close)
        write_config(src)
        eid = str(entities.create_entity(
            src, name="Invented Kiln LLC", structure="sole_prop",
            owner="spouse")["id"])
        restore.restore_zip(dest, export.build_zip(src))
        got = dest.execute("SELECT owner FROM business_entity WHERE id = %s",
                           (eid,)).fetchone()
        self.assertEqual(got["owner"], "spouse")
        self.assertIn("owner", landers.LANDERS["business_entity"].columns)
        # an archive from before the column, or a value outside the three
        self.assertEqual(landers._entity_owner(None), "unknown")
        self.assertEqual(landers._entity_owner("cousin"), "unknown")
        self.assertEqual(landers._entity_owner("primary"), "primary")

    def test_the_plaintext_ein_is_never_restored(self):
        """The schema keeps only the last four. An older backup can carry
        a full EIN column, and reading it back would store it again."""
        # the documents land through their declared lander
        from oikonome.sync.landers import LANDERS
        written = LANDERS["income_documents"].columns
        self.assertIn("ein_last4", written)
        self.assertNotIn("ein", written)

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
