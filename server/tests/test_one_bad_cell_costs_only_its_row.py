"""One archived cell the database will not take costs only its own row.

The restore is one transaction, so any write the database refused (text
in a uuid column, a whole number past what an INTEGER holds) used to roll
back every other row of the archive. Each row is now judged against the
columns it writes, as the catalogue declares them, before the write: a
cell a required column cannot take refuses its row (malformed, the
column and its type named in the summary, never the value), and one an
optional column cannot take is left empty. No savepoint is opened: a long
restore holding thousands of them would overflow the subtransaction
cache, which slows every session on the server.

The page's own rules apply too: an optional figure the page would refuse
(text where a number belongs, a share outside 0-100, a link without
http(s), a member id that is no id) is left empty and its row lands; a
business movement of a kind the page does not record, or of no positive
amount, is refused.

And a household holding an alert under both its full and its cut
spelling has both muted by the archive's dismissal.
"""
import unittest

from oikonome.sync import restore

from .util import make_db

from .test_a_restore_refuses_one_bad_row_never_the_archive import (
    ENT, ENT2, _Case, _entity, _uid)


class AColumnRefusesOneRow(_Case):

    def test_a_refused_row_leaves_the_rest_of_its_batch_landed(self):
        def txn(i, **over):
            return {"id": f"bc-{i}", "account_id": "chk",
                    "date": "2026-04-01", "amount": "12",
                    "name": "Corner Cafe", "removed": "0", **over}
        counts, out = self._restore(transactions=[
            txn(1), txn(2, removed="3000000000"), txn(3)])
        self.assertEqual(sorted(r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'bc-%'")),
            ["bc-1", "bc-3"])
        self.assertEqual(out.refused.get(("transactions", "malformed")), 1)
        self.assertIsNone(out.refused.get(("transactions", "present")))
        self.assertIn("a removed that is not a valid integer",
                      " | ".join(counts.get("_notes") or []))

    def test_an_id_that_is_no_uuid_refuses_its_row(self):
        _, out = self._restore(business_entity=[
            _entity(), _entity("not-a-uuid")])
        self.assertEqual(self._ids("business_entity"), [ENT])
        self.assertEqual(out.refused.get(("business_entity", "malformed")),
                         1)


class ABoundedTextColumnTakesWhatPostgresTakes(unittest.TestCase):
    """A varchar(n)/char(n) column takes a value past n when the excess is
    only trailing spaces (Postgres trims them), so the restore must not
    refuse or empty it; past n by anything else it is refused."""

    def test_trailing_spaces_past_the_bound_are_taken(self):
        for udt in ("varchar", "bpchar"):
            col = {"udt_name": udt, "chars": 3, "precision": None}
            with self.subTest(udt=udt):
                self.assertTrue(restore._takes(col, "abc   "))
                self.assertTrue(restore._takes(col, "ab"))
                self.assertFalse(restore._takes(col, "abcd"))
                self.assertFalse(restore._takes(col, "abcd  "))
        conn = make_db()
        try:
            # what the rule mirrors: an INSERT trims trailing spaces past
            # the bound and refuses anything else past it
            conn.execute("CREATE TEMP TABLE fit_probe (v varchar(3))")
            conn.execute("INSERT INTO fit_probe VALUES (%s)", ("abc   ",))
            self.assertEqual(conn.execute(
                "SELECT v FROM fit_probe").fetchone()["v"], "abc")
        finally:
            conn.close()


class AnOptionalFigureThePageRefusesIsLeftEmpty(_Case):

    def test_the_business_and_its_member_land_without_it(self):
        counts, out = self._restore(
            business_entity=[_entity(income_tax_rate="abc",
                                     home_office_sqft="3000000000")],
            entity_membership=[
                {"id": _uid(901), "entity_id": ENT, "member_name": "A",
                 "ownership_pct": "x"},
                {"id": _uid(902), "entity_id": ENT, "member_name": "B",
                 "ownership_pct": "150"}],
            equity_movement=[
                {"id": _uid(903), "entity_id": ENT, "kind": "draw",
                 "amount": "5", "date": "2026-04-01",
                 "member_id": "not-a-member"}],
            compliance_obligation=[
                {"id": _uid(904), "entity_id": ENT, "title": "Filing",
                 "due_date": "2026-06-30", "url": "sos.example.test"}])
        ent = self.conn.execute(
            "SELECT income_tax_rate, home_office_sqft FROM business_entity"
            " WHERE id=%s", (ENT,)).fetchone()
        self.assertEqual(dict(ent), {"income_tax_rate": None,
                                     "home_office_sqft": None})
        self.assertEqual(
            [r["ownership_pct"] for r in self.conn.execute(
                "SELECT ownership_pct FROM entity_membership ORDER BY id")],
            [None, None])
        self.assertIsNone(self.conn.execute(
            "SELECT member_id FROM equity_movement WHERE id=%s",
            (_uid(903),)).fetchone()["member_id"])
        self.assertIsNone(self.conn.execute(
            "SELECT url FROM compliance_obligation WHERE id=%s",
            (_uid(904),)).fetchone()["url"])
        self.assertEqual(out.total("value_emptied"), 6)
        self.assertEqual(out.total("malformed"), 0)

    def test_the_business_pages_rules_judge_each_field(self):
        """A rate outside 0-100, a home office that is negative or not
        whole, a fiscal year end that is no day, a status the page never
        sets and a tax reserve that is not one of the business's own
        accounts are each left out; the business lands."""
        counts, out = self._restore(
            business_entity=[
                _entity(income_tax_rate="150", home_office_sqft="-5",
                        fiscal_year_end="13-45", status="frozen",
                        tax_reserve_account_id="chk"),
                _entity(ENT2, income_tax_rate="20",
                        home_office_sqft="12.7")])
        rows = {str(r["id"]): r for r in self.conn.execute(
            "SELECT id, income_tax_rate, home_office_sqft, fiscal_year_end,"
            " status, tax_reserve_account_id FROM business_entity")}
        self.assertEqual(
            {k: rows[ENT][k] for k in ("income_tax_rate", "home_office_sqft",
                                       "fiscal_year_end", "status",
                                       "tax_reserve_account_id")},
            {"income_tax_rate": None, "home_office_sqft": None,
             "fiscal_year_end": None, "status": "active",
             "tax_reserve_account_id": None})
        self.assertIsNone(rows[ENT2]["home_office_sqft"])
        self.assertEqual(float(rows[ENT2]["income_tax_rate"]), 20.0)
        self.assertEqual(out.total("value_emptied"), 6)

    def test_a_fiscal_year_end_saved_as_free_text_lands_as_the_day(self):
        """A business saved before the page checked the field could carry
        '12/31' or '6-30'; the restore reads such a day as the page writes
        it (MM-DD) rather than blanking it."""
        _, out = self._restore(business_entity=[
            _entity(fiscal_year_end="12/31"),
            _entity(ENT2, fiscal_year_end=" 6-30 ")])
        rows = {str(r["id"]): r["fiscal_year_end"] for r in self.conn.execute(
            "SELECT id, fiscal_year_end FROM business_entity")}
        self.assertEqual(rows, {ENT: "12-31", ENT2: "06-30"})
        self.assertEqual(out.total("value_emptied"), 0)

    def test_a_business_without_a_name_is_refused(self):
        _, out = self._restore(business_entity=[
            _entity(), _entity(ENT2, name="   ")])
        self.assertEqual(self._ids("business_entity"), [ENT])
        self.assertEqual(out.refused.get(("business_entity", "malformed")),
                         1)


class AMovementFollowsThePagesRules(_Case):

    def test_an_unknown_kind_or_no_positive_amount_is_refused(self):
        def mv(n, **over):
            return {"id": _uid(n), "entity_id": ENT, "kind": "draw",
                    "amount": "5", "date": "2026-04-01", **over}
        _, out = self._restore(
            business_entity=[_entity()],
            equity_movement=[mv(911), mv(912, kind="gift"),
                             mv(913, amount="0"), mv(914, amount="-5"),
                             mv(915, amount="five")])
        self.assertEqual(self._ids("equity_movement"), [_uid(911)])
        self.assertEqual(out.refused.get(("equity_movement", "malformed")),
                         4)


class ATripFollowsThePagesRules(_Case):

    def test_a_trip_of_no_positive_miles_or_no_number_is_refused(self):
        def trip(n, miles):
            return {"id": _uid(n), "entity_id": ENT, "date": "2026-04-01",
                    "miles": miles}
        _, out = self._restore(
            business_entity=[_entity()],
            mileage_log=[trip(921, "500"), trip(922, "-500"),
                         trip(923, "0"), trip(924, "far")])
        self.assertEqual(self._ids("mileage_log"), [_uid(921)])
        self.assertEqual(out.refused.get(("mileage_log", "malformed")), 3)


class ARestoreOpensNoSavepoint(_Case):

    def test_every_write_runs_in_the_one_transaction(self):
        """Counted at the connection: any transaction() opened while one is
        already open is a savepoint. The merchant pass that runs after the
        restore has committed is its own work, in its own transactions,
        and is left out."""
        from psycopg import pq
        nested = []
        real = type(self.conn).transaction

        def counting(conn, *a, **kw):
            if conn.info.transaction_status != pq.TransactionStatus.IDLE:
                nested.append(1)
            return real(conn, *a, **kw)

        def txn(i, **over):
            return {"id": f"sp-{i}", "account_id": "chk",
                    "date": "2026-04-01", "amount": "12",
                    "name": "Corner Cafe", **over}
        from unittest import mock
        from oikonome.engine import merchant_identity
        with mock.patch.object(type(self.conn), "transaction", counting), \
                mock.patch.object(merchant_identity, "resolve"):
            self._restore(
                business_entity=[_entity(), _entity("not-a-uuid")],
                transactions=[txn(1), txn(2, removed="3000000000"), txn(3)],
                entity_membership=[
                    {"id": _uid(931), "entity_id": ENT, "member_name": "A",
                     "ownership_pct": "40"}],
                mileage_log=[{"id": _uid(932), "entity_id": ENT,
                              "date": "2026-04-01", "miles": "3"}])
        self.assertEqual(nested, [])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE id LIKE 'sp-%'"
        ).fetchone()["n"], 2)


class BothSpellingsOfAnAlertTakeTheMute(_Case):

    def test_a_dismissal_mutes_the_full_and_the_cut_spelling(self):
        full = "Card 4111111111111111 balance is high"
        cut = restore._alert_identity("balance", full)[1]
        self.assertNotEqual(full, cut)
        for msg in (full, cut):
            self.conn.execute(
                "INSERT INTO alerts_log (kind, severity, message, first_seen,"
                " last_seen, active, dismissed) VALUES ('balance', 'warn',"
                " %s, '2026-04-01', '2026-04-02', 1, 0)", (msg,))
        self._restore(alerts_log=[
            {"kind": "balance", "severity": "warn", "message": full,
             "first_seen": "2026-04-01", "last_seen": "2026-04-02",
             "active": "1", "dismissed": "1"}])
        self.assertEqual(
            [r["dismissed"] for r in self.conn.execute(
                "SELECT dismissed FROM alerts_log WHERE kind='balance'")],
            [1, 1])

    def test_one_spelling_already_muted_still_mutes_the_other(self):
        full = "Card 4111111111111111 balance is high"
        cut = restore._alert_identity("balance", full)[1]
        for msg, dismissed in ((full, 1), (cut, 0)):
            self.conn.execute(
                "INSERT INTO alerts_log (kind, severity, message, first_seen,"
                " last_seen, active, dismissed) VALUES ('balance', 'warn',"
                " %s, '2026-04-01', '2026-04-02', 1, %s)", (msg, dismissed))
        _, out = self._restore(alerts_log=[
            {"kind": "balance", "severity": "warn", "message": full,
             "first_seen": "2026-04-01", "last_seen": "2026-04-02",
             "active": "1", "dismissed": "1"}])
        self.assertEqual(
            sorted(r["dismissed"] for r in self.conn.execute(
                "SELECT dismissed FROM alerts_log WHERE kind='balance'")),
            [1, 1])
        self.assertEqual(out.written.get("alerts_log"), 1)


class TheBoundIsTheColumns(_Case):
    """What `_numeric` lets through is what the column stores: every
    boundary value sent through psycopg to the database itself."""

    CASES = ((999.995, 5, 2), (999.994, 5, 2), (-999.995, 5, 2),
             (-999.994, 5, 2), (999.9995, 6, 3), (999.9994, 6, 3),
             (-999.9995, 6, 3), (999999999.95, 10, 1),
             (999999999.94, 10, 1), (-999999999.95, 10, 1),
             (999999999999.995, 14, 2), (999999999999.99, 14, 2),
             (-999999999999.995, 14, 2), (0.005, 5, 2), (-0.005, 5, 2))

    def test_the_helper_and_the_column_agree_at_every_edge(self):
        import psycopg
        for v, p, s in self.CASES:
            with self.subTest(v=v, p=p, s=s):
                try:
                    with self.conn.transaction():
                        self.conn.execute(
                            f"SELECT %s::numeric({p},{s})", (v,))
                    column_takes = True
                except psycopg.errors.NumericValueOutOfRange:
                    column_takes = False
                try:
                    restore._numeric(repr(v), "a figure", p, s)
                    helper_takes = True
                except restore._AmountOutOfRange:
                    helper_takes = False
                self.assertEqual(helper_takes, column_takes)


if __name__ == "__main__":
    unittest.main()
