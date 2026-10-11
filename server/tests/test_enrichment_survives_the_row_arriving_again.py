"""What Enrich learned about a row survives the row arriving again.

Enrich is billed per row sent, and it picks the rows whose raw carries no
enrichment marker. Every source rewrites raw when it sends a row again: a
re-imported overlapping statement (file ids are content-hashed, so it hits
the same rows) and a feed that re-sends its window every pull. Without the
marker those rows go back to the front of the Enrich queue and are paid for
a second time.
"""
import datetime as dt
import unittest

from oikonome.sync import base as sync_base, batches, csvimport, plaid
from oikonome.sync.base import Transaction

from .util import make_db, write_config

MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}
CSV = ("Date,Amount,Description\n"
       "2026-03-02,-18.40,RIVERSIDE CAFE\n"
       "2026-03-03,-64.10,CORNER GROCER\n")


def _enrich(conn, ids):
    conn.execute(
        "UPDATE transactions SET raw = raw || jsonb_build_object("
        "'_enriched_at', '2026-03-05T00:00:00+00:00', "
        "'personal_finance_category', jsonb_build_object("
        "'primary', 'FOOD_AND_DRINK'), 'website', 'riverside.example') "
        "WHERE id = ANY(%s)", (ids,))


class EnrichmentSurvives(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _candidates(self):
        return {r["id"] for r in plaid.enrich_candidates(self.conn)}

    def _import(self):
        bid = batches.create(self.conn, "csv", "chk", "march.csv")
        csvimport.import_csv(self.conn, "chk", CSV, MAPPING, batch_id=bid)
        return bid

    def test_a_reimported_statement_keeps_its_enrichment(self):
        self._import()
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'csv:%'")]
        self.assertEqual(len(ids), 2)
        _enrich(self.conn, ids)
        self.assertFalse(self._candidates() & set(ids))
        second = self._import()
        self.assertFalse(self._candidates() & set(ids))
        for r in self.conn.execute(
                "SELECT raw FROM transactions WHERE id = ANY(%s)", (ids,)):
            self.assertEqual(r["raw"]["website"], "riverside.example")
            self.assertEqual(r["raw"]["_batch"], second)
            self.assertEqual(len(r["raw"]["_batches"]), 2)

    def test_a_feed_resending_a_row_keeps_its_enrichment(self):
        row = Transaction(id="sfin:a:1", account_id="chk",
                          date=dt.date(2026, 3, 2), amount=18.40,
                          name="RIVERSIDE CAFE", raw={"memo": "pos"})
        sync_base.upsert_transactions(self.conn, [row])
        _enrich(self.conn, ["sfin:a:1"])
        sync_base.upsert_transactions(self.conn, [row])
        raw = self.conn.execute("SELECT raw FROM transactions "
                                "WHERE id='sfin:a:1'").fetchone()["raw"]
        self.assertIn("_enriched_at", raw)
        self.assertEqual(raw["memo"], "pos")
        self.assertNotIn("sfin:a:1", self._candidates())

    def test_a_feed_resending_a_row_keeps_its_outcome_unknown_mark(self):
        """A row whose Enrich request may have been billed with the answer
        lost is held out of Enrich for a day by a mark in raw. The feed
        re-sends its window every pull; the mark must survive that, or the
        row is offered, and paid for, again on the next run. The rest of
        raw is still the feed's to restate."""
        row = Transaction(id="sfin:a:2", account_id="chk",
                          date=dt.date(2026, 3, 3), amount=64.10,
                          name="CORNER GROCER", raw={"memo": "pos"})
        sync_base.upsert_transactions(self.conn, [row])
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE transactions SET raw = raw || jsonb_build_object("
            "'_enrich_unknown_at', %s::text) WHERE id='sfin:a:2'", (stamp,))
        self.assertNotIn("sfin:a:2", self._candidates())
        again = Transaction(id="sfin:a:2", account_id="chk",
                            date=dt.date(2026, 3, 3), amount=64.10,
                            name="CORNER GROCER", raw={"memo": "pos card"})
        sync_base.upsert_transactions(self.conn, [again])
        raw = self.conn.execute("SELECT raw FROM transactions "
                                "WHERE id='sfin:a:2'").fetchone()["raw"]
        self.assertEqual(raw["_enrich_unknown_at"], stamp)
        self.assertEqual(raw["memo"], "pos card")
        self.assertNotIn("_enriched_at", raw)
        self.assertNotIn("sfin:a:2", self._candidates())

    def test_every_key_enrich_writes_is_one_the_upsert_keeps(self):
        self.assertLessEqual({dst for _, dst in plaid.ENRICH_FIELDS},
                             set(sync_base.ENRICHED_RAW_KEYS))


if __name__ == "__main__":
    unittest.main()
