"""The two dates every window opens from are read with one-row index probes.
They must be the dates a scan of the whole ledger gives, under every rule
that decides which rows count, and a request must not pay for them twice."""

import datetime as dt
import unittest

from oikonome.db import tenancy
from oikonome.engine import budget, links, reporting
from oikonome.engine.reporting import (
    _NOT_SHADOW, SPEND_WHERE, excluded_accounts_sql, _ledger_anchor_dates)

from .util import add_txn, make_db, write_config


def whole_ledger_scan(conn):
    """The definition, spelled as the whole-ledger aggregate it replaces."""
    excl, ex_p = excluded_accounts_sql(conn)
    row = conn.execute(
        f"""WITH spend AS (
                SELECT t.account_id, t.date FROM transactions t
                 WHERE {SPEND_WHERE} {excl})
            SELECT (SELECT MIN(date) FROM spend) AS d,
                   (SELECT MIN(t.date) FROM transactions t
                     WHERE t.removed = 0 {excl}
                       {_NOT_SHADOW.format(col="t.account_id")}
                       {budget.PERSONAL_ONLY_SQL}
                       AND t.account_id IN (SELECT account_id FROM spend)
                   ) AS cov""",
        (*ex_p, *ex_p) or None).fetchone()
    return row["d"], row["cov"]


class AnchorsMatchTheScan(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def assert_agree(self, expect=None):
        self.conn._memo = None
        tenancy.set_entity_scope(self.conn)
        links.set_shadow_scope(self.conn)
        got = _ledger_anchor_dates(self.conn, None)
        self.assertEqual(got, whole_ledger_scan(self.conn))
        if expect is not None:
            self.assertEqual(got, expect)

    def _account(self, aid, kind="depository"):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype) VALUES "
            "(%s,'it1',%s,%s,'x')", (aid, aid, kind))

    def test_an_empty_ledger_has_neither_date(self):
        self.assert_agree((None, None))

    def test_a_deposit_before_the_first_purchase_opens_coverage_only(self):
        add_txn(self.conn, dt.date(2024, 3, 1), -50.0, "DEPOSIT",
                primary="INCOME", account="chk")
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "SHOP", account="chk")
        self.assert_agree((dt.date(2024, 3, 9), dt.date(2024, 3, 1)))

    def test_an_account_with_no_spend_cannot_open_coverage(self):
        self._account("plan")
        add_txn(self.conn, dt.date(2010, 1, 5), -50.0, "CONTRIBUTION",
                primary="INCOME", account="plan")
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "SHOP")
        self.assert_agree((dt.date(2024, 3, 9), dt.date(2024, 3, 9)))

    def test_only_transfers_and_card_payments_have_neither_date(self):
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "MOVE",
                primary="TRANSFER_OUT", account="chk")
        add_txn(self.conn, dt.date(2024, 3, 9), 30.0, "PAY",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", account="chk")
        self.assert_agree((None, None))

    def test_a_loan_account_is_not_spend(self):
        self._account("loan1", "loan")
        add_txn(self.conn, dt.date(2020, 3, 9), 20.0, "INTEREST", account="loan1")
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "SHOP")
        self.assert_agree((dt.date(2024, 3, 9), dt.date(2024, 3, 9)))

    def test_removed_rows_decide_nothing(self):
        add_txn(self.conn, dt.date(2020, 3, 9), 20.0, "GONE", txn_id="gone")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id='gone'")
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "SHOP")
        self.assert_agree((dt.date(2024, 3, 9), dt.date(2024, 3, 9)))

    def test_an_excluded_account_decides_nothing(self):
        self._account("old")
        add_txn(self.conn, dt.date(2019, 3, 9), 20.0, "OLD", account="old")
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "SHOP")
        write_config(self.conn, excluded_accounts=["old"])
        self.assert_agree((dt.date(2024, 3, 9), dt.date(2024, 3, 9)))

    def test_a_hidden_account_decides_nothing(self):
        self._account("old")
        add_txn(self.conn, dt.date(2019, 3, 9), 20.0, "OLD", account="old")
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "SHOP")
        self.conn.execute("UPDATE accounts SET user_removed_at=now() WHERE id='old'")
        self.assert_agree((dt.date(2024, 3, 9), dt.date(2024, 3, 9)))

    def test_a_shadow_copy_decides_nothing(self):
        self.conn.execute(
            "INSERT INTO items (id,aggregator,institution_name,access_token) "
            "VALUES ('it2','test','Twin','tok')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype) VALUES "
            "('twin','it2','Twin','depository','checking')")
        add_txn(self.conn, dt.date(2019, 3, 9), 20.0, "OLD", account="twin")
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "SHOP", account="chk")
        links.create(self.conn, ["chk", "twin"])
        self.assert_agree((dt.date(2024, 3, 9), dt.date(2024, 3, 9)))

    def _business(self, combine=False):
        eid = self.conn.execute(
            "INSERT INTO business_entity (name, structure) VALUES "
            "('Fabrikam LLC','single_member_llc') RETURNING id").fetchone()["id"]
        self._account("biz")
        self.conn.execute("UPDATE accounts SET entity_id=%s WHERE id='biz'", (eid,))
        add_txn(self.conn, dt.date(2019, 3, 9), 20.0, "OLD", account="biz")
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "SHOP")
        if combine:
            write_config(self.conn, combine_entities=True)

    def test_business_money_decides_nothing(self):
        self._business()
        self.assert_agree((dt.date(2024, 3, 9), dt.date(2024, 3, 9)))

    def test_business_money_counts_when_the_household_combines_it(self):
        self._business(combine=True)
        self.assert_agree((dt.date(2019, 3, 9), dt.date(2019, 3, 9)))

    def test_a_row_tagged_business_on_a_personal_account_decides_nothing(self):
        eid = self.conn.execute(
            "INSERT INTO business_entity (name, structure) VALUES "
            "('Fabrikam LLC','single_member_llc') RETURNING id").fetchone()["id"]
        add_txn(self.conn, dt.date(2019, 3, 9), 20.0, "OLD", txn_id="tagged")
        self.conn.execute("UPDATE transactions SET entity_id=%s WHERE id='tagged'",
                          (eid,))
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "SHOP")
        self.assert_agree((dt.date(2024, 3, 9), dt.date(2024, 3, 9)))

    def test_a_row_with_no_date_decides_nothing(self):
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "SHOP")
        self.conn.execute(
            "INSERT INTO transactions (id,account_id,date,amount,name) "
            "VALUES ('undated','card',NULL,5,'UNDATED')")
        self.assert_agree((dt.date(2024, 3, 9), dt.date(2024, 3, 9)))

    def test_a_spend_row_with_no_date_still_marks_its_account(self):
        self.conn.execute(
            "INSERT INTO transactions (id,account_id,date,amount,name) "
            "VALUES ('undated','chk',NULL,5,'UNDATED')")
        add_txn(self.conn, dt.date(2024, 3, 1), -50.0, "DEPOSIT",
                primary="INCOME", account="chk")
        self.assert_agree((None, dt.date(2024, 3, 1)))


class AnchorsAreReadOncePerCheckout(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        add_txn(self.conn, dt.date(2024, 3, 9), 20.0, "SHOP")

    def _statements_during(self, fn):
        seen = []
        cls = type(self.conn)
        real = cls.execute

        def spy(conn, *a, **kw):
            seen.append(a[0] if a else kw.get("query"))
            return real(conn, *a, **kw)
        cls.execute = spy
        try:
            out = fn()
        finally:
            cls.execute = real
        return out, [q for q in seen if "FROM transactions" in str(q)]

    def test_the_second_ask_in_a_checkout_reads_no_ledger(self):
        first = _ledger_anchor_dates(self.conn, None)
        again, queries = self._statements_during(
            lambda: _ledger_anchor_dates(self.conn, None))
        self.assertEqual(again, first)
        self.assertEqual(queries, [])

    def test_a_new_charge_is_seen_at_once(self):
        _ledger_anchor_dates(self.conn, None)
        add_txn(self.conn, dt.date(2021, 3, 9), 20.0, "OLDER")
        self.assertEqual(_ledger_anchor_dates(self.conn, None),
                         (dt.date(2021, 3, 9), dt.date(2021, 3, 9)))

    def test_an_exclusion_is_seen_at_once(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype) VALUES "
            "('old','it1','old','depository','x')")
        add_txn(self.conn, dt.date(2021, 3, 9), 20.0, "OLD", account="old")
        self.assertEqual(_ledger_anchor_dates(self.conn, None)[0],
                         dt.date(2021, 3, 9))
        write_config(self.conn, excluded_accounts=["old"])
        self.assertEqual(_ledger_anchor_dates(self.conn, None)[0],
                         dt.date(2024, 3, 9))

    def test_hiding_an_account_is_seen_at_once(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype) VALUES "
            "('old','it1','old','depository','x')")
        add_txn(self.conn, dt.date(2021, 3, 9), 20.0, "OLD", account="old")
        _ledger_anchor_dates(self.conn, None)
        self.conn.execute("UPDATE accounts SET user_removed_at=now() WHERE id='old'")
        links.set_shadow_scope(self.conn)
        self.assertEqual(_ledger_anchor_dates(self.conn, None)[0],
                         dt.date(2024, 3, 9))

    def test_a_named_account_list_is_not_answered_from_the_memo(self):
        _ledger_anchor_dates(self.conn, None)
        add_txn(self.conn, dt.date(2022, 3, 9), -20.0, "IN",
                primary="INCOME", account="chk")
        self.assertEqual(_ledger_anchor_dates(self.conn, ["chk"])[0],
                         dt.date(2022, 3, 9))

    def test_a_year_lens_style_request_runs_the_probe_at_most_once(self):
        """The direct callers all reach the anchors through _income_coverage,
        with a bank feed that opens past the grace day."""
        add_txn(self.conn, dt.date(2024, 3, 20), -500.0, "PAY",
                primary="INCOME", account="chk")

        def request():
            reporting._income_coverage(self.conn)
            reporting._income_coverage(self.conn)
            reporting._range_window(self.conn, dt.date(2024, 9, 1), "3m", None)
        self.conn._memo = None
        _, queries = self._statements_during(request)
        probes = [q for q in queries if "ORDER BY t.date" in str(q)
                  or "WITH spend" in str(q)]
        self.assertEqual(sum("WITH spend" in str(q) for q in probes), 0)
        # the spend day and the coverage day: three statements in all, of
        # which these two order by date, and each runs once
        self.assertEqual(len(probes), 2, probes)


if __name__ == "__main__":
    unittest.main()
