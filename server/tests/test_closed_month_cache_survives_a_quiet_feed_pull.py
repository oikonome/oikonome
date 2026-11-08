"""A bank pull that moved nothing the Cash Flow total reads must not throw
away the closed-month cache, and a change to anything it does read must.

The second half is the one that matters: a stale closed-month total on the
page is worse than a slow page. So the columns are not listed by hand here.
Every column the schema has is walked, and a column is either fingerprinted
or named as something the feed rewrites and the total does not read."""

import datetime as dt
import decimal
import json
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import links, reporting
from oikonome.sync.base import Account, upsert_accounts

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 9, 1)


def _new_value(data_type: str, current):
    """A value of the column's type that differs from `current`."""
    if data_type == "uuid":
        return str(uuid.uuid4())
    if data_type in ("text", "character varying"):
        return f"{current or ''}-moved"
    if data_type == "boolean":
        return not current
    if data_type in ("integer", "bigint", "smallint"):
        return (current or 0) + 1
    if data_type in ("double precision", "numeric", "real"):
        return float(current or 0) + 1.5
    if data_type == "timestamp with time zone":
        return dt.datetime(2020, 1, 2, tzinfo=dt.timezone.utc)
    if data_type == "date":
        return dt.date(2020, 1, 2)
    if data_type == "jsonb":
        return json.dumps({"moved": True})
    raise AssertionError(f"no test value for a column of type {data_type}")


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        reporting._FIXED_MONTH_CACHE.clear()
        self.addCleanup(reporting._FIXED_MONTH_CACHE.clear)
        for month in range(3, 9):
            day = dt.date(2026, month, 10)
            add_txn(self.conn, day, 1500.0, "RENT",
                    primary="RENT_AND_UTILITIES", account="chk")
            add_txn(self.conn, day, 40.0, "GROCER", primary="FOOD_AND_DRINK")
            add_txn(self.conn, day, -5000.0, "ACME PAYROLL", primary="INCOME",
                    account="chk")
        add_bill(self.conn, "RENT", 1500.0, next_due=dt.date(2026, 9, 10))
        add_bill(self.conn, "ACME PAYROLL", 5000.0,
                 next_due=dt.date(2026, 9, 5), income=True)

    def stamp(self, cfg=None):
        return reporting._fixed_inputs_stamp(self.conn, cfg or {})

    def refresh_scope(self):
        """What the next request's fresh checkout would do."""
        tenancy.set_entity_scope(self.conn)
        links.set_shadow_scope(self.conn)
        self.conn._memo = None

    def assert_cache_follows(self, change):
        """Warm the cache, make the change, and require the figures the
        cache serves to be the ones a cold walk computes."""
        reporting._FIXED_MONTH_CACHE.clear()
        before = reporting.flow_breakdown(self.conn, TODAY, "3m")
        change()
        self.refresh_scope()
        served = reporting.flow_breakdown(self.conn, TODAY, "3m")
        reporting._FIXED_MONTH_CACHE.clear()
        cold = reporting.flow_breakdown(self.conn, TODAY, "3m")
        self.assertEqual((served["in"], served["out"]),
                         (cold["in"], cold["out"]),
                         "a closed month served the figure from before the change")
        return before, cold


class QuietPullKeepsTheCache(_Base):
    def _pull(self, balance=5000.0):
        """Everything a Plaid pull with no new charges writes."""
        upsert_accounts(self.conn, "it1", [
            Account(id="chk", name="Test Checking", type="depository",
                    subtype="checking", mask=None, balance_current=balance,
                    balance_available=balance, currency=None,
                    raw={"pulled_at": str(uuid.uuid4())})])
        self.conn.execute(
            "UPDATE items SET tx_cursor=%s, status='ok', "
            "tx_update_status='HISTORICAL_UPDATE_COMPLETE', "
            "bank_updated_at=now(), bank_failed_at=now(), webhook_status=NULL,"
            " webhook_status_at=NULL WHERE id='it1'", (uuid.uuid4().hex,))

    def test_a_pull_with_no_new_charges_leaves_the_stamp_alone(self):
        before = self.stamp()
        self._pull()
        self.assertEqual(self.stamp(), before)

    def test_a_pull_that_moved_a_balance_leaves_the_stamp_alone(self):
        before = self.stamp()
        self._pull(balance=4321.0)
        self.assertEqual(self.stamp(), before)

    def test_the_closed_months_are_not_walked_again_after_a_quiet_pull(self):
        from unittest import mock
        from oikonome.engine import budget
        reporting.flow_breakdown(self.conn, TODAY, "3m")
        self._pull()
        with mock.patch.object(budget, "month_status",
                               wraps=budget.month_status) as walked:
            reporting.flow_breakdown(self.conn, TODAY, "3m")
        self.assertEqual(walked.call_count, 0, "closed months walked again")


class EveryColumnIsAnInputOrNamedAsNot(_Base):
    """Walks the schema, so a column a later migration adds is covered the
    day it lands, not whenever someone remembers this cache."""

    def _columns(self, table):
        return [(r["column_name"], r["data_type"]) for r in self.conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (table,))]

    def _move(self, table, row_id, col, data_type):
        current = self.conn.execute(
            f"SELECT {col} AS v FROM {table} WHERE id=%s",
            (row_id,)).fetchone()["v"]
        if isinstance(current, decimal.Decimal):
            current = float(current)
        cast = "::jsonb" if data_type == "jsonb" else ""
        self.conn.execute(f"UPDATE {table} SET {col}=%s{cast} WHERE id=%s",
                          (_new_value(data_type, current), row_id))

    def _walk(self, table, row_id, not_inputs, frozen=()):
        columns = dict(self._columns(table))
        self.assertLessEqual(set(not_inputs), set(columns),
                             "a named column no longer exists")
        for col, data_type in columns.items():
            if col in frozen:
                continue
            with self.subTest(column=f"{table}.{col}"):
                before = self.stamp()
                self._move(table, row_id, col, data_type)
                self.conn._memo = None
                if col in not_inputs:
                    self.assertEqual(self.stamp(), before)
                else:
                    self.assertNotEqual(self.stamp(), before)

    def test_every_accounts_column_moves_the_stamp_unless_named(self):
        self._walk("accounts", "card", reporting._FIXED_NOT_INPUTS["accounts"],
                   # identity and foreign keys: moved below, by the behaviour
                   # that needs them, because a free UPDATE would break them
                   frozen=("tenant_id", "id", "item_id", "entity_id"))

    def test_every_items_column_moves_the_stamp_unless_named(self):
        self._walk("items", "it1", reporting._FIXED_NOT_INPUTS["items"],
                   frozen=("tenant_id", "id"))

    def test_the_named_columns_do_not_change_the_closed_month_total(self):
        """The other half of the bargain: what the stamp ignores really is
        not read. Move every ignored column at once on every row; a cold
        walk must give the same figures to the cent."""
        cold = reporting.flow_breakdown(self.conn, TODAY, "all")
        for table, rows in (("accounts", ("chk", "card")), ("items", ("it1",))):
            columns = dict(self._columns(table))
            for row_id in rows:
                for col in reporting._FIXED_NOT_INPUTS[table]:
                    self._move(table, row_id, col, columns[col])
        self.refresh_scope()
        reporting._FIXED_MONTH_CACHE.clear()
        moved = reporting.flow_breakdown(self.conn, TODAY, "all")
        self.assertEqual((moved["in"], moved["out"]), (cold["in"], cold["out"]))


class TheInputsInvalidateTheCache(_Base):
    """Each column the total reads, moved the way the product moves it, with
    a figure that really changes, so a pass here is the page being right."""

    def _second_item(self):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, access_token)"
            " VALUES ('it2','plaid','Northwind Brokerage','tok')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype) VALUES "
            "('brk','it2','Northwind IRA','investment','ira')")

    def test_hiding_an_account(self):
        before, cold = self.assert_cache_follows(lambda: self.conn.execute(
            "UPDATE accounts SET user_removed_at=now() WHERE id='chk'"))
        self.assertNotEqual(before["out"], cold["out"])

    def test_assigning_an_account_to_a_business(self):
        def change():
            eid = self.conn.execute(
                "INSERT INTO business_entity (name, structure) "
                "VALUES ('Fabrikam LLC', 'single_member_llc') RETURNING id").fetchone()["id"]
            self.conn.execute("UPDATE accounts SET entity_id=%s WHERE id='chk'",
                              (eid,))
        before, cold = self.assert_cache_follows(change)
        self.assertNotEqual(before["out"], cold["out"])

    def test_reclassifying_an_account_as_a_loan(self):
        before, cold = self.assert_cache_follows(lambda: self.conn.execute(
            "UPDATE accounts SET type='loan' WHERE id='chk'"))
        self.assertNotEqual(before["out"], cold["out"])

    def test_moving_an_account_into_a_brokerage_login(self):
        """accounts.item_id decides brokerage cash: a deposit on it counts
        only when stamped INCOME."""
        self._second_item()
        add_txn(self.conn, dt.date(2026, 8, 3), -700.0, "SWEEP",
                primary="OTHER", account="chk")
        before, cold = self.assert_cache_follows(lambda: self.conn.execute(
            "UPDATE accounts SET item_id='it2' WHERE id='chk'"))
        self.assertNotEqual(before["in"], cold["in"])

    def test_a_new_account_row(self):
        before = self.stamp()
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype) VALUES "
            "('new','it1','New','depository','savings')")
        self.assertNotEqual(self.stamp(), before)

    def test_a_deleted_account_row(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype) VALUES "
            "('new','it1','New','depository','savings')")
        before = self.stamp()
        self.conn.execute("DELETE FROM accounts WHERE id='new'")
        self.assertNotEqual(self.stamp(), before)

    def _card_pay_month(self):
        """A bill whose payee reads as a card payment claims this charge
        only while no live card debt exists to be paid by the card
        machinery."""
        self.conn.execute("UPDATE accounts SET balance_current=0 WHERE id='card'")
        add_bill(self.conn, "CARD PAYMENT", 900.0,
                 next_due=dt.date(2026, 9, 20))
        add_txn(self.conn, dt.date(2026, 8, 20), 900.0, "CARD PAYMENT",
                primary="GENERAL_SERVICES", account="chk")

    def test_card_debt_appearing_changes_the_total_and_misses(self):
        self._card_pay_month()
        before, cold = self.assert_cache_follows(lambda: self.conn.execute(
            "UPDATE accounts SET balance_current=250 WHERE id='card'"))
        self.assertNotEqual(before["out"]["fixed"], cold["out"]["fixed"])

    def test_card_debt_clearing_changes_the_total_and_misses(self):
        self._card_pay_month()
        self.conn.execute("UPDATE accounts SET balance_current=250 WHERE id='card'")
        before, cold = self.assert_cache_follows(lambda: self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'"))
        self.assertNotEqual(before["out"]["fixed"], cold["out"]["fixed"])

    def test_card_debt_moving_without_crossing_zero_keeps_the_stamp(self):
        self._card_pay_month()
        self.conn.execute("UPDATE accounts SET balance_current=250 WHERE id='card'")
        before = self.stamp()
        self.conn.execute("UPDATE accounts SET balance_current=999 WHERE id='card'")
        self.assertEqual(self.stamp(), before)

    def test_an_excluded_card_is_not_live_debt(self):
        """The drop is judged under the config the walk runs under."""
        self._card_pay_month()
        self.conn.execute("UPDATE accounts SET balance_current=250 WHERE id='card'")
        self.assertNotEqual(self.stamp({}),
                            self.stamp({"excluded_accounts": ["card"]}))

    def test_a_feed_error_on_a_linked_primary_fails_over_and_misses(self):
        """items.status reaches the total through the effective shadow set."""
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, access_token)"
            " VALUES ('it2','test','Twin','tok')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype) VALUES "
            "('chk2','it2','Twin','depository','checking')")
        add_txn(self.conn, dt.date(2026, 8, 25), 77.0, "ONLY ON THE TWIN",
                primary="GENERAL_SERVICES", account="chk2")
        links.create(self.conn, ["chk", "chk2"])
        before, cold = self.assert_cache_follows(lambda: self.conn.execute(
            "UPDATE items SET status='error:ITEM_LOGIN_REQUIRED' WHERE id='it1'"))
        self.assertNotEqual(before["out"], cold["out"])

    def test_the_clock_failing_a_linked_primary_over_still_misses(self):
        """accounts.updated_at is ignored as a column because a pull rewrites
        it, and read through the effective shadow set because a primary
        whose feed went quiet stops being primary."""
        self.conn.execute("UPDATE items SET aggregator='plaid' WHERE id='it1'")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, access_token)"
            " VALUES ('it2','plaid','Twin','tok')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,updated_at) "
            "VALUES ('chk2','it2','Twin','depository','checking',now())")
        self.conn.execute("UPDATE accounts SET updated_at=now() WHERE id='chk'")
        add_txn(self.conn, dt.date(2026, 8, 25), 77.0, "ONLY ON THE TWIN",
                primary="GENERAL_SERVICES", account="chk2")
        links.create(self.conn, ["chk", "chk2"])
        before, cold = self.assert_cache_follows(lambda: self.conn.execute(
            "UPDATE accounts SET updated_at=now() - interval '30 days' "
            "WHERE id='chk'"))
        self.assertNotEqual(before["out"], cold["out"])


if __name__ == "__main__":
    unittest.main()
