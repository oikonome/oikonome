"""A capitalized or reimbursable cost leaves the capital account with the
charge the bank feed withdrew.

The books stop counting a withdrawn charge. The movement explaining who
funded it would otherwise keep crediting the member's capital (or keep the
business owing them) for a payment that never happened. The nightly
re-anchor sweep releases it on the same terms as a withdrawn reimbursement
pair: only once the reconcile window has passed with no re-numbered twin.
"""
import unittest

from oikonome.engine import budget
from oikonome.sync import adopt, base, reanchor

from .util import TODAY, add_txn, make_db, write_config


class WithdrawnFrontedTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.ent = self.conn.execute(
            "INSERT INTO business_entity (name, structure)"
            " VALUES ('Example Studio LLC', 'single_member_llc')"
            " RETURNING id").fetchone()["id"]
        self.charge = add_txn(self.conn, TODAY, 80.0, "NORTHWIND HOSTING")
        self.conn.execute(
            "INSERT INTO equity_movement (entity_id, kind, amount, date,"
            " txn_id) VALUES (%s,'contribution',80.0,%s,%s)",
            (self.ent, TODAY, self.charge))
        self.assertEqual(base.mark_removed(self.conn, [self.charge]), 1)

    def _age(self, days):
        self.conn.execute(
            "UPDATE transactions SET raw = raw || jsonb_build_object("
            "%s::text, now() - make_interval(days => %s)) WHERE id = %s",
            (budget.FEED_REMOVED_KEY, days, self.charge))

    def _movements(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM equity_movement WHERE txn_id=%s",
            (self.charge,)).fetchone()["n"]

    def test_the_sweep_waits_then_releases_the_movement(self):
        out = reanchor.reanchor_stranded(
            self.conn, release_after_days=adopt.RECONCILE_DAYS)
        self.assertEqual(out["fronted_released"], 0)
        self.assertEqual(self._movements(), 1)
        self._age(adopt.RECONCILE_DAYS + 1)
        out = reanchor.reanchor_stranded(
            self.conn, release_after_days=adopt.RECONCILE_DAYS)
        self.assertEqual(out["fronted_released"], 1)
        self.assertEqual(self._movements(), 0)

    def test_a_twin_carries_the_movement_instead(self):
        self._age(adopt.RECONCILE_DAYS + 1)
        twin = add_txn(self.conn, TODAY, 80.0, "NORTHWIND HOSTING")
        out = reanchor.reanchor_stranded(
            self.conn, release_after_days=adopt.RECONCILE_DAYS)
        self.assertEqual(out["fronted_released"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM equity_movement WHERE txn_id=%s",
            (twin,)).fetchone()["n"], 1)

    def test_a_past_year_end_sheet_stops_counting_it_at_once(self):
        # the year-end package rebuilds the capital account itself; it
        # counts the same movements the live one does, so a withdrawn
        # charge's movement drops out before the sweep releases it
        import datetime as dt

        from oikonome.web import api
        today = dt.date.today()
        last = dt.date(today.year - 1, 6, 1)
        self.conn.execute("UPDATE equity_movement SET date=%s"
                          " WHERE txn_id=%s", (last, self.charge))
        self.conn.execute(
            "INSERT INTO equity_movement (entity_id, kind, amount, date)"
            " VALUES (%s,'contribution',50.0,%s)", (self.ent, last))
        sheet = api._year_end_sheet(self.conn, str(self.ent),
                                    today.year - 1, today, [])
        self.assertEqual(sheet["capital_account"], 50.0)


if __name__ == "__main__":
    unittest.main()
