"""A raw record restored from an archive can hold a value of the wrong type
where the aggregator always sends text — an entity id that is a number or
a list, a name that is not a string. Read as it stands, the merchant pass
crashed on that one row; the row stayed unresolved, so it crashed again
every night, and because the pass was not isolated it took the rest of the
night with it (split repair, twin fold, the categorizer, the receipt
sweeps, the nightly heartbeat). Pinned: such a field reads as absent, and
a merchant step that fails costs only itself.
"""
import unittest
from unittest import mock

from oikonome.engine import merchant_dedup, merchant_identity
from oikonome.jobs import worker

from .test_llm_categorize import add_raw_txn
from .util import make_db


class MalformedRawIdentityTests(unittest.TestCase):
    def test_wrong_typed_fields_read_as_absent(self):
        pi = merchant_identity.plaid_identity
        self.assertIsNone(pi({"counterparties": [
            {"type": "merchant", "entity_id": ["x"], "name": "A"}]}))
        self.assertIsNone(pi({"counterparties": [
            {"type": "merchant", "entity_id": 123, "name": "A"}]}))
        self.assertIsNone(pi({"merchant_entity_id": 123}))
        self.assertIsNone(pi({"counterparties": "not a list"}))
        got = pi({"counterparties": [
            {"type": ["merchant"], "entity_id": "e1"},
            {"type": "merchant", "entity_id": "e2", "name": 7,
             "confidence_level": ["HIGH"], "logo_url": {"u": 1}}]})
        self.assertEqual(got["entity_id"], "e2")
        self.assertIsNone(got["name"])
        self.assertIsNone(got["logo_url"])

    def test_a_good_record_reads_as_before(self):
        got = merchant_identity.plaid_identity({"counterparties": [
            {"type": "merchant", "entity_id": "ent_1", "name": "Northwind",
             "confidence_level": "HIGH", "website": "northwind.example"}]})
        self.assertEqual((got["entity_id"], got["name"], got["kind"],
                          got["website"]),
                         ("ent_1", "Northwind", "merchant",
                          "northwind.example"))


class MalformedRawLedgerTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def tearDown(self):
        self.conn.close()

    def test_the_merchant_pass_resolves_a_malformed_row_by_name(self):
        add_raw_txn(self.conn, "bad1", "2025-07-01", 10, "NORTHWIND",
                    raw={"counterparties": [{"type": "merchant",
                                             "entity_id": ["x"],
                                             "name": "Northwind"}]})
        add_raw_txn(self.conn, "bad2", "2025-07-02", 11, "QUILLO",
                    raw={"merchant_entity_id": 123})
        merchant_dedup.apply(self.conn)
        for tid in ("bad1", "bad2"):
            self.assertIsNotNone(self.conn.execute(
                "SELECT merchant_id FROM transactions WHERE id=%s",
                (tid,)).fetchone()["merchant_id"], tid)

    def test_a_failing_merchant_step_costs_the_night_only_itself(self):
        with mock.patch("oikonome.engine.merchant_dedup.apply",
                        side_effect=TypeError("unhashable")), \
             mock.patch("oikonome.engine.amazon_match.run_match",
                        side_effect=ValueError("bad")), \
             mock.patch("oikonome.engine.merchant_identity.fold_twins",
                        return_value={"folded": 0}) as fold, \
             mock.patch("oikonome.engine.receipts.parse_pending") as rp:
            worker.nightly_tenant(self.tid)
        fold.assert_called_once()
        rp.assert_called_once()
        self.assertIsNotNone(self.conn.execute(
            "SELECT ran_at FROM job_runs WHERE job = 'nightly-detect'"
        ).fetchone(), "the nightly-detect heartbeat never ran")


if __name__ == "__main__":
    unittest.main()
