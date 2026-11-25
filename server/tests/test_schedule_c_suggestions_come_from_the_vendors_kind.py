"""Schedule C suggestions come from what the vendor is — its merchant
category code or the words in its name — and from the owner's own earlier
lines, never from the bank's category; a saved line that is only the old
category guess accepted is not learned and repeated
(specs/business-books.md → Suggested lines)."""
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import books, entities
from oikonome.engine.business_kinds import kind_for

from .util import _admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts, TODAY


class KindTests(unittest.TestCase):
    def test_a_vendors_kind_names_its_line(self):
        self.assertEqual(kind_for("Example Cloud Hosting"),
                         ("software and hosting", "Other expenses"))
        self.assertEqual(kind_for("Example Social Ads"),
                         ("advertising", "Advertising"))
        self.assertEqual(kind_for("Example Bank Wire Fee"),
                         ("bank fees", "Other expenses"))
        self.assertEqual(kind_for("Example State SOS"),
                         ("government filing fees", "Taxes and licenses"))
        # the merchant's registered code wins over its name
        self.assertEqual(kind_for("Example Ads Studio", mcc="7372"),
                         ("software and hosting", "Other expenses"))
        self.assertIsNone(kind_for("Example Vendor"))
        # whole words only: "roads" is not "ads"
        self.assertIsNone(kind_for("Example Roads Co"))


class SuggestionSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"kind-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(self.conn, name="Example Co",
                                          structure="sole_prop")["id"]
        entities.assign_account(self.conn, "chk", self.ent)

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def _suggest(self):
        return {s["txn_id"]: s for s in books.suggest_lines(self.conn,
                                                             self.ent)}

    def test_a_hosting_bill_the_bank_calls_general_services_is_software(self):
        t = add_txn(self.conn, TODAY, 12.0, "Example Cloud Hosting",
                    account="chk", primary="GENERAL_SERVICES")
        s = self._suggest()[t]
        self.assertEqual((s["suggested_line"], s["source"], s["kind_label"]),
                         ("Other expenses", "kind", "software and hosting"))

    def test_an_accepted_category_guess_is_not_learned(self):
        # an earlier row saved on the line the bank category used to
        # suggest: not the owner's own choice, so not repeated as "learned"
        old = add_txn(self.conn, TODAY, 20.0, "Example Ads Network",
                      account="chk", primary="GENERAL_SERVICES")
        books.classify(self.conn, old, "operating",
                       sched_c_line="Legal and professional services")
        new = add_txn(self.conn, TODAY, 25.0, "Example Ads Network",
                      account="chk", primary="GENERAL_SERVICES")
        s = self._suggest()[new]
        self.assertEqual((s["suggested_line"], s["source"]),
                         ("Advertising", "kind"))
        # a line the owner chose that no guess offered is learned
        books.classify(self.conn, old, "operating", sched_c_line="Supplies")
        s = self._suggest()[new]
        self.assertEqual((s["suggested_line"], s["source"]),
                         ("Supplies", "merchant"))


if __name__ == "__main__":
    unittest.main()
