"""An entity field is offered as a setting only when something reads it.

A PATCH that accepts a value and stores it reads, to whoever sets it, as a
working setting. The self-employment estimate takes no filing status, so a
writable `filing_status` would change no figure anywhere while looking like
it should. Fields the continuity document prints (registered agent, fiscal
year end) stay writable.
"""

import unittest

from oikonome.engine import entities

from .util import _ensure_db, make_db


class EntitySettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        self.ent = entities.create_entity(
            self.conn, name="Northwind LLC", structure="single_member_llc")

    def tearDown(self):
        self.conn.close()

    def test_filing_status_is_not_a_setting(self):
        out = entities.update_entity(self.conn, self.ent["id"],
                                     filing_status="single")
        self.assertNotIn("filing_status", out)
        stored = self.conn.execute(
            "SELECT filing_status FROM business_entity WHERE id = %s",
            (self.ent["id"],)).fetchone()["filing_status"]
        self.assertIsNone(stored)

    def test_continuity_fields_stay_writable(self):
        out = entities.update_entity(self.conn, self.ent["id"],
                                     registered_agent="Example Agent Co",
                                     fiscal_year_end="06-30")
        self.assertEqual(out["registered_agent"], "Example Agent Co")
        self.assertEqual(out["fiscal_year_end"], "06-30")


if __name__ == "__main__":
    unittest.main()
