"""An account the household switched off with the Accounts page's "excl"
toggle is out of the cash runway everywhere it is out of the reports.

Two invariants, one rule. First: the runway's STARTING CHECKING BALANCE
and its CARD DEBT both drop excluded accounts. An excluded high-balance
account left linked would otherwise start the 60-day walk from money every
other figure on the page ignores, hiding the household running out of
cash; an excluded card would be warned about on the Today tile and in the
nightly email while the forecast chart beside them has already dropped it.
Second: the Today tile and the forecast read the SAME card scope, so the
tile's debt and the chart's can never disagree about one household.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, forecast
from oikonome.web import report

from .util import TODAY, add_bill, make_db, write_config


def _add_card(conn, acct_id: str, name: str, balance: float) -> None:
    conn.execute(
        "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
        " VALUES (%s,'it1',%s,'credit','credit card',%s)",
        (acct_id, name, balance))


class ExcludedAccountsLeaveTheRunwayTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()          # checking 'chk' $5,000, card 'cc' $250

    def tearDown(self):
        self.conn.close()

    def test_auto_picked_checking_skips_an_excluded_account(self):
        # a dormant account with the biggest balance, switched off
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES "
            "('chk_old','it1','Dormant Checking','depository','checking',"
            "45000,45000)")
        write_config(self.conn, excluded_accounts=["chk_old"])
        cfg = budget.load_config(self.conn)
        self.assertEqual(forecast._checking_balance(self.conn, cfg), 5000)

    def test_pinned_checking_that_is_excluded_falls_through(self):
        # the household pinned an account and later switched it off: the
        # two settings contradict each other, and the exclusion — the one
        # every report already obeys — wins
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES "
            "('chk_old','it1','Dormant Checking','depository','checking',"
            "45000,45000)")
        write_config(self.conn, checking_account_id="chk_old",
                     excluded_accounts=["chk_old"])
        cfg = budget.load_config(self.conn)
        self.assertEqual(forecast._checking_balance(self.conn, cfg), 5000)

    def test_today_card_debt_matches_the_forecast(self):
        _add_card(self.conn, "cc_biz", "Second Card", 8000)
        write_config(self.conn, excluded_accounts=["cc_biz"])
        status = report.gather(self.conn, TODAY)
        fc = forecast.build(self.conn, TODAY)
        # the excluded card is gone from the tile, its list of cards, and
        # the cash the household is told it needs on hand
        self.assertEqual(status["runway"]["card_debt"], fc["card_debt"])
        self.assertEqual(status["runway"]["card_debt"], 250)
        self.assertNotIn("Second Card",
                         [c[0] for c in status["runway"]["cards"]])
        self.assertLess(status["runway"]["required"], 8000)


class CardPayBillSurvivesAnExcludedCardTests(unittest.TestCase):
    """A hand-tracked "card payment" bill is dropped only when the card
    machinery will pay that debt on a cash surface instead. An EXCLUDED
    card schedules no payoff event, so its balance must not delete the
    bill: the payment would then appear on no cash surface at all —
    neither as a bill occurrence nor as a card event — which is the
    failure the drop rule exists to prevent.
    """

    def setUp(self):
        self.conn = make_db()          # checking 'chk', card 'card' $250
        add_bill(self.conn, "CREDIT CARD AUTOPAY", 600.0,
                 next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25))

    def tearDown(self):
        self.conn.close()

    def _occurrence_payees(self) -> set[str]:
        cfg = budget.load_config(self.conn)
        return {o["payee"] for o in budget.upcoming_bill_occurrences(
            self.conn, cfg, TODAY, TODAY + dt.timedelta(days=30))}

    def test_an_excluded_cards_balance_does_not_delete_the_bill(self):
        self.conn.execute(
            "UPDATE accounts SET balance_current = 8000 WHERE id = 'card'")
        write_config(self.conn, excluded_accounts=["card"])
        self.assertIn("CREDIT CARD AUTOPAY", self._occurrence_payees())
        # and nothing replaces it on the forecast side either — which is
        # why deleting it would lose the payment outright
        self.assertEqual(forecast.build(self.conn, TODAY)["card_debt"], 0)

    def test_live_card_debt_still_deletes_the_bill(self):
        # the rule itself is unchanged: a card the machinery DOES pay owns
        # the payment, so the hand-tracked bill stands down
        write_config(self.conn)
        self.assertNotIn("CREDIT CARD AUTOPAY", self._occurrence_payees())


class MalformedExclusionListTests(unittest.TestCase):
    """The settings blob is a document a whole-ledger restore writes
    verbatim, so a value of any type can land under `excluded_accounts`.
    `value or []` substitutes only on a FALSY value, so a `true` or a
    number reaches `list()` unchanged and raises `TypeError: not
    iterable` — every report, the runway and the calendar strip 500 at
    once, with no UI to take the value back out. It must read as
    "nothing excluded" instead.
    """

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _store(self, value) -> None:
        """Plant the value the way a restore does — straight into the
        stored document, bypassing the toggle that would have written a
        list."""
        write_config(self.conn)
        from oikonome.engine.compat import jsonb
        cfg = dict(budget.load_config(self.conn))
        cfg["excluded_accounts"] = value
        self.conn.execute("UPDATE tenant_settings SET config = %s",
                          (jsonb(cfg),))

    def test_a_non_list_reads_as_nothing_excluded(self):
        for value in (True, 7, "chk", {"chk": 1}):
            with self.subTest(value=value):
                self.assertEqual(
                    budget.excluded_account_ids({"excluded_accounts": value}),
                    [])

    def test_every_money_surface_survives_a_non_list(self):
        from oikonome.engine import anomalies, reporting
        for value in (True, 7, "chk"):
            with self.subTest(value=value):
                self._store(value)
                fc = forecast.build(self.conn, TODAY)
                self.assertEqual(fc["card_debt"], 250)
                self.assertEqual(reporting.excluded_accounts_sql(self.conn),
                                 ("", ()))
                anomalies.detect(self.conn, TODAY)
                budget.month_status(self.conn, TODAY)
                self.assertEqual(
                    report.gather(self.conn, TODAY)["runway"]["card_debt"],
                    250)

    def test_restore_coerces_the_list_it_is_handed(self):
        from oikonome.sync import restore
        cfg = {"excluded_accounts": True}
        restore.clean_excluded_accounts(cfg)
        self.assertNotIn("excluded_accounts", cfg)
        cfg = {"excluded_accounts": ["chk", 4, None, {"a": 1}]}
        restore.clean_excluded_accounts(cfg)
        self.assertEqual(cfg["excluded_accounts"], ["chk", "4"])


class YearBucketUsesTheHouseholdDayTests(unittest.TestCase):
    """Which months a year's money-map bucket has to sum is a calendar
    question, so it is answered on the household's day. On the container's
    UTC clock a west-coast evening on the 31st has already opened the next
    month, and the year's last bucket would be summed one month wider than
    the map that links to it."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_default_day_is_the_households_not_the_containers(self):
        from unittest import mock

        from oikonome import localtime
        from oikonome.web import lenses
        with mock.patch.object(localtime, "household_day",
                               return_value=dt.date(2026, 7, 15)) as day:
            self.assertIsNone(lenses.year_bucket_ledger(
                self.conn, "no such bucket", 2026))
        day.assert_called_once_with(self.conn)


if __name__ == "__main__":
    unittest.main()
