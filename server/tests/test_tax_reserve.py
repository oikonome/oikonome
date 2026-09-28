"""An entity can nominate the account its tax money sits in.

Measured against ALL business cash, "Set aside for tax" answers "is the
money there", not "is it ring-fenced". Setting estimated tax aside in a
separate account is ordinary practice for a self-employed filer, and the app
already computes the figure — so the entity records where that money lives.
"""

import unittest
import uuid

from oikonome.engine import entities

from .util import make_db


class TaxReserveTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _entity(self, name=None):
        return entities.create_entity(
            self.conn, name=name or f"Biz {uuid.uuid4().hex[:6]}",
            structure="sole_prop")

    def _account(self, entity_id=None, aid=None):
        aid = aid or f"acct-{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name) "
            "VALUES (%s,'manual','Test Bank') ON CONFLICT DO NOTHING",
            ("item-tax",))
        self.conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype,
                                     balance_current, entity_id)
               VALUES (%s,'item-tax','Tax Savings','depository','savings',
                       %s,%s)""",
            (aid, 5000.0, entity_id))
        return aid

    def test_it_starts_unset(self):
        e = self._entity()
        self.assertIsNone(e.get("tax_reserve_account_id"))

    def test_an_assigned_account_can_be_nominated(self):
        e = self._entity()
        a = self._account(entity_id=e["id"])
        out = entities.update_entity(self.conn, e["id"],
                                     tax_reserve_account_id=a)
        self.assertEqual(out["tax_reserve_account_id"], a)

    def test_a_personal_account_is_refused(self):
        """Nominating an account this business does not own would misreport
        the set-aside for the business AND the household."""
        e = self._entity()
        personal = self._account(entity_id=None)
        with self.assertRaises(ValueError) as cm:
            entities.update_entity(self.conn, e["id"],
                                   tax_reserve_account_id=personal)
        self.assertIn("assigned to this business", str(cm.exception))

    def test_another_entitys_account_is_refused(self):
        a_ent, b_ent = self._entity(), self._entity()
        theirs = self._account(entity_id=b_ent["id"])
        with self.assertRaises(ValueError):
            entities.update_entity(self.conn, a_ent["id"],
                                   tax_reserve_account_id=theirs)

    def test_an_unknown_id_is_refused(self):
        e = self._entity()
        with self.assertRaises(ValueError):
            entities.update_entity(self.conn, e["id"],
                                   tax_reserve_account_id="no-such-account")

    def test_empty_clears_the_nomination(self):
        """The picker's own empty option. "" and "no reserve account" are
        the same thing and the column should not have to tell them apart."""
        e = self._entity()
        a = self._account(entity_id=e["id"])
        entities.update_entity(self.conn, e["id"], tax_reserve_account_id=a)
        out = entities.update_entity(self.conn, e["id"],
                                     tax_reserve_account_id="")
        self.assertIsNone(out["tax_reserve_account_id"])

    def test_it_is_returned_to_the_client(self):
        e = self._entity()
        a = self._account(entity_id=e["id"])
        entities.update_entity(self.conn, e["id"], tax_reserve_account_id=a)
        got = entities.get_entity(self.conn, e["id"])
        self.assertEqual(got["tax_reserve_account_id"], a)

    def test_the_ein_ciphertext_still_never_travels(self):
        """The public column list grew; the thing it exists to exclude must
        not have slipped in with it."""
        self.assertNotIn("ein_enc", entities._PUBLIC)
