"""The assistant read tools say what the pages say, and nothing more.

What breaks if these fail: a search tool's totals disagree with the
Transactions page for the same filter, a month or category breakdown no
longer adds up to the total it breaks down, a filter the tool advertises
is silently ignored, a card number in a merchant name reaches an
assistant whole, an aggregator's own account id leaves the server, or a
period that starts before an account's history reads as a period with no
spending in it.
"""

from __future__ import annotations

import datetime as dt
import unittest
import unittest.mock

from oikonome.engine import ledger_read as lr
from oikonome.engine import reporting, splits
from oikonome.web import data

from .util import TODAY, add_txn, make_db, write_config

FOOD, OTHER, HOME = "FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "HOME_IMPROVEMENT"


def _search(conn, top_merchants_all=False, **kw):
    args = lr.parse_args("search_transactions", kw)
    if top_merchants_all:
        args["_top"] = None          # every merchant, for the sum invariant
    return lr.search_transactions(conn, args)


class Seeded(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        c = self.conn
        c.execute("UPDATE accounts SET mask = '7788', owner = 'Robin' "
                  "WHERE id = 'card'")
        last = TODAY.replace(day=1) - dt.timedelta(days=1)
        self.t_market = add_txn(c, TODAY.replace(day=3), 64.0, "LANTERN GROCERY",
                                primary=FOOD)
        add_txn(c, TODAY.replace(day=4), 12.5, "LANTERN GROCERY", primary=FOOD)
        self.t_split = add_txn(c, TODAY.replace(day=6), 120.0, "FERNHILL GOODS",
                               primary=OTHER, account="chk")
        splits.set_split(c, self.t_split, [{"category": OTHER, "amount": 40},
                                           {"category": HOME, "amount": 80}])
        add_txn(c, last.replace(day=20), 30.0, "OTTER CAFE", primary=FOOD)
        add_txn(c, last.replace(day=12), -900.0, "ACME PAYROLL", primary="INCOME",
                account="chk")
        add_txn(c, TODAY.replace(day=2), -900.0, "ACME PAYROLL", primary="INCOME",
                account="chk")
        add_txn(c, TODAY.replace(day=7), 250.0, "CARD PAYMENT",
                primary="LOAN_PAYMENTS", account="chk",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        self.start = last.replace(day=1)
        self.end = TODAY


class TotalsTests(Seeded):

    def test_totals_are_cash_flow_s_figures_for_the_same_days(self):
        got = _search(self.conn, start_date=self.start.isoformat(),
                      end_date=self.end.isoformat())
        want = reporting.flows(self.conn, start=self.start,
                               end=self.end + dt.timedelta(days=1))
        self.assertEqual(got["totals"]["out"], round(want["out"], 2))
        self.assertEqual(got["totals"]["in"], round(want["in"], 2))
        self.assertEqual(got["totals"]["net"], round(want["net"], 2))
        self.assertEqual(got["totals"]["count"], 7)

    def test_totals_are_the_page_search_s_over_every_match_not_the_page(self):
        got = _search(self.conn, query="lantern", limit=1)
        _rows, total, _s, _a, spend, _h = data.search_transactions(
            self.conn, "lantern", flows=True)
        self.assertEqual(len(got["transactions"]), 1)
        self.assertTrue(got["has_more"])
        self.assertEqual(got["totals"]["count"], total)
        self.assertEqual(got["totals"]["out"], round(spend["flows"]["out"], 2))
        self.assertEqual(got["totals"]["out"], 76.5)

    def test_months_add_up_to_the_totals_to_the_cent(self):
        c = self.conn
        for i in range(9):
            add_txn(c, TODAY.replace(day=1) - dt.timedelta(days=40 * i + 3),
                    10.335 + i / 1000, "ODD CENTS")
            add_txn(c, TODAY.replace(day=1) - dt.timedelta(days=40 * i + 5),
                    -20.115 - i / 1000, "ODD REFUND", primary="INCOME",
                    account="chk")
        got = _search(c)
        for k in ("out", "in", "net"):
            self.assertEqual(round(sum(m[k] for m in got["by_month"]), 2),
                             got["totals"][k], k)

    def test_merchants_add_up_to_money_out_under_every_filter(self):
        filters = [{}, {"category": HOME}, {"category": OTHER},
                   {"category": FOOD}, {"category": "?"},
                   {"query": "fernhill"}, {"category": HOME, "query": "goods"},
                   {"owner": "Robin"}, {"direction": "out", "min_amount": 20}]
        for f in filters:
            got = _search(self.conn, top_merchants_all=True, **f)
            self.assertAlmostEqual(
                sum(m["amount"] for m in got["top_merchants"]),
                got["totals"]["out"], places=2, msg=f)

    def test_months_and_categories_add_up_to_the_total(self):
        got = _search(self.conn, start_date=self.start.isoformat())
        self.assertAlmostEqual(sum(m["out"] for m in got["by_month"]),
                               got["totals"]["out"], places=2)
        self.assertAlmostEqual(sum(m["in"] for m in got["by_month"]),
                               got["totals"]["in"], places=2)
        self.assertAlmostEqual(sum(c["amount"] for c in got["by_category"]),
                               got["totals"]["out"], places=2)
        self.assertEqual([m["month"] for m in got["by_month"]],
                         sorted(m["month"] for m in got["by_month"]))

    def test_a_split_charge_lands_in_each_part_s_category(self):
        got = _search(self.conn, start_date=TODAY.replace(day=1).isoformat())
        cats = {c["category"]: c["amount"] for c in got["by_category"]}
        self.assertEqual(cats.get(HOME), 80.0)
        self.assertEqual(cats.get(OTHER), 40.0)
        only = _search(self.conn, category=HOME)
        self.assertEqual(only["totals"]["out"], 80.0)
        self.assertEqual([c["category"] for c in only["by_category"]], [HOME])

    def test_the_month_breakdown_matches_flows_month_by_month(self):
        months = reporting.flows_by_month(self.conn)
        for m in months:
            y, mo = map(int, m["month"].split("-"))
            a = dt.date(y, mo, 1)
            b = (a + dt.timedelta(days=32)).replace(day=1)
            f = reporting.flows(self.conn, start=a, end=b)
            self.assertEqual((m["out"], m["in"]), (f["out"], f["in"]), m)

    def test_top_merchants_are_folded_and_sorted(self):
        got = _search(self.conn, direction="out")
        names = [m["merchant"] for m in got["top_merchants"]]
        self.assertEqual(names[0], "FERNHILL GOODS")
        lantern = next(m for m in got["top_merchants"]
                       if m["merchant"] == "LANTERN GROCERY")
        self.assertEqual((lantern["amount"], lantern["count"]), (76.5, 2))


class _Counting:
    """A connection that counts its statements and remembers the longest
    list any of them was handed as a parameter."""

    def __init__(self, conn):
        self.conn, self.n, self.longest = conn, 0, 0

    def execute(self, query, params=None, *a, **k):
        self.n += 1
        for p in (params.values() if isinstance(params, dict) else params or ()):
            if isinstance(p, (list, tuple)):
                self.longest = max(self.longest, len(p))
        return self.conn.execute(query, params, *a, **k)

    def __getattr__(self, name):
        return getattr(self.conn, name)


class CostTests(Seeded):
    """An unfiltered search over the whole ledger costs a fixed number of
    statements, and none of its totals or groupings is computed by
    shipping the matched id list back into a query — on a large ledger
    that list is the expensive part."""

    # get_overview's accounts, the search's id list, its count, the page
    # and its accounts, Cash Flow's two halves, the two month halves, the
    # categories, the merchants, the count of rows the totals leave out,
    # and the config / exclusion / combined-view lookups they each make
    PINNED = 26

    def _count(self) -> int:
        # the first call after the ledger changes also reloads what the
        # connection then keeps (the household's settings and caches), so
        # it is the warm-up
        lr.search_transactions(self.conn, lr.parse_args("search_transactions", {}))
        cc = _Counting(self.conn)
        got = lr.search_transactions(cc, lr.parse_args("search_transactions", {}))
        self.assertLessEqual(cc.longest, lr.LIMIT_DEFAULT, "an id list was sent")
        self.assertTrue(got["has_more"])
        return cc.n

    def test_the_statement_count_is_pinned_and_independent_of_size(self):
        for i in range(60):
            add_txn(self.conn, TODAY - dt.timedelta(days=i), 5.0 + i, f"BULK {i % 7}")
        small = self._count()
        for i in range(240):
            add_txn(self.conn, TODAY - dt.timedelta(days=i % 400), 2.0 + i,
                    f"MORE {i % 11}")
        self.assertEqual((small, self._count()), (self.PINNED, self.PINNED))


class OneSnapshotTests(Seeded):
    """A sync that commits while a call runs must not reach half of it."""

    def test_a_write_committed_mid_call_reaches_none_of_its_figures(self):
        from oikonome.db import tenancy
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        other = tenancy.tenant_connect(tid)
        self.addCleanup(other.close)
        real = self.conn.execute
        seen = {"n": 0}

        def execute(query, *a, **k):
            seen["n"] += 1
            if seen["n"] == 8:          # between the id list and the groupings
                add_txn(other, TODAY.replace(day=9), 500.0, "LATE ARRIVAL",
                        primary=FOOD)
            return real(query, *a, **k)
        before = _search(self.conn)
        with lr.one_snapshot(self.conn), \
                unittest.mock.patch.object(self.conn, "execute", execute):
            got = lr.search_transactions(
                self.conn, lr.parse_args("search_transactions", {}))
        self.assertGreater(seen["n"], 8)
        for k in ("count", "out", "in", "net"):
            self.assertEqual(got["totals"][k], before["totals"][k], k)
        self.assertEqual(got["by_month"], before["by_month"])
        self.assertEqual(got["by_category"], before["by_category"])
        after = _search(self.conn)
        self.assertEqual(after["totals"]["out"], before["totals"]["out"] + 500)

    def test_months_absorb_rounding_only(self):
        months = [{"month": "2026-01", "out": 100.0, "in": 0.0},
                  {"month": "2026-02", "out": 50.0, "in": 0.0}]
        # a cent of rounding goes to the largest month
        fixed = lr._months_to_the_cent(months, 150.01, 0.0)
        self.assertEqual([m["out"] for m in fixed], [100.01, 50.0])
        # a real difference is never pushed into a month
        left = lr._months_to_the_cent(months, 650.0, 0.0)
        self.assertEqual([m["out"] for m in left], [100.0, 50.0])


class TotalsOutsideTests(Seeded):
    """Rows the household's totals leave out are listed and counted, and
    the reply says so instead of presenting a $0 total as the answer."""

    def _ids(self):
        return {a["name"]: a for a in lr.get_overview(self.conn, {})["accounts"]}

    def test_an_excluded_account_is_marked_and_its_rows_are_counted_out(self):
        write_config(self.conn, excluded_accounts=["card"])
        accts = self._ids()
        self.assertIs(accts["Test Card"]["counted_in_totals"], False)
        self.assertEqual(accts["Test Card"]["not_counted_because"], "excluded")
        self.assertIs(accts["Test Checking"]["counted_in_totals"], True)
        got = _search(self.conn, account_ids=[accts["Test Card"]["id"]])
        self.assertEqual(got["totals"]["count"], 3)
        self.assertEqual(got["totals"]["out"], 0.0)
        self.assertEqual(got["left_out_of_totals"], 3)
        self.assertIn("leave out", got["note"])

    def test_a_business_account_is_marked_and_counted_out(self):
        eid = self.conn.execute(
            "INSERT INTO business_entity (name, structure) VALUES ('Workshop', 'sole_prop')"
            " RETURNING id").fetchone()["id"]
        self.conn.execute("UPDATE accounts SET entity_id = %s WHERE id = 'card'",
                          (eid,))
        from oikonome.db import tenancy
        tenancy.set_entity_scope(self.conn)
        accts = self._ids()
        self.assertEqual(accts["Test Card"]["not_counted_because"], "business")
        got = _search(self.conn, account_ids=[accts["Test Card"]["id"]])
        self.assertEqual(got["left_out_of_totals"], 3)
        self.assertIn("business", got["note"])

    def test_nothing_left_out_says_nothing(self):
        got = _search(self.conn)
        self.assertEqual(got["left_out_of_totals"], 0)
        self.assertNotIn("note", got)


class PagingCapTests(Seeded):

    def test_has_more_stops_at_the_paging_limit_with_a_note(self):
        with unittest.mock.patch.object(lr, "OFFSET_MAX", 4):
            got = lr.search_transactions(self.conn, {
                **lr.parse_args("search_transactions", {}), "limit": 2,
                "offset": 4})
        self.assertFalse(got["has_more"])
        self.assertIn("narrow", got["note"])

    def test_an_offset_past_the_limit_says_to_narrow_by_date(self):
        with self.assertRaises(lr.ToolInputError) as cm:
            lr.parse_args("search_transactions", {"offset": lr.OFFSET_MAX + 1})
        self.assertIn("start_date", str(cm.exception))


class RoundTripTests(Seeded):
    """Every category and merchant a search returns, passed back as its
    filter, totals what the grouping said."""

    def setUp(self):
        super().setUp()
        c = self.conn
        # the same category stored both ways, a name holding a long number
        # and spaces a reader cannot see, an owner label of the same kind
        add_txn(c, TODAY.replace(day=8), 70.0, "PINE  RENTAL 12345678901",
                override="RENT AND UTILITIES", account="chk")
        add_txn(c, TODAY.replace(day=9), 30.0, "PINE  RENTAL 12345678901",
                primary="RENT_AND_UTILITIES", account="chk")
        add_txn(c, TODAY.replace(day=10), 15.0, "LANTERN GROCERY EAST",
                primary=FOOD)
        c.execute("UPDATE accounts SET owner = 'Sam  and Robin 12345678' "
                  "WHERE id = 'chk'")

    def test_categories_round_trip(self):
        got = _search(self.conn)
        self.assertTrue(got["by_category"])
        for cat in got["by_category"]:
            again = _search(self.conn, category=cat["category"])
            self.assertEqual(again["totals"]["out"], cat["amount"], cat)

    def test_merchants_round_trip(self):
        got = _search(self.conn, top_merchants_all=True)
        names = [m["merchant"] for m in got["top_merchants"]]
        self.assertIn("LANTERN GROCERY", names)
        self.assertIn("LANTERN GROCERY EAST", names)
        for m in got["top_merchants"]:
            again = _search(self.conn, merchant=m["merchant"])
            self.assertEqual(again["totals"]["out"], m["amount"], m)

    def test_owners_round_trip(self):
        ov = lr.get_overview(self.conn, {})
        for owner in ov["owners"]:
            got = _search(self.conn, owner=owner)
            self.assertTrue(got["transactions"], owner)


class FilterTests(Seeded):

    def _payees(self, **kw):
        return sorted(t["payee"] for t in _search(self.conn, **kw)["transactions"])

    def test_direction(self):
        self.assertEqual(self._payees(direction="in"), ["ACME PAYROLL"] * 2)
        self.assertNotIn("ACME PAYROLL", self._payees(direction="out"))

    def test_amount_bounds_are_on_the_size(self):
        self.assertEqual(self._payees(min_amount=800), ["ACME PAYROLL"] * 2)
        self.assertEqual(self._payees(max_amount=20), ["LANTERN GROCERY"])

    def test_merchant_and_owner(self):
        self.assertEqual(self._payees(merchant="LANTERN GROCERY"),
                         ["LANTERN GROCERY"] * 2)
        self.assertEqual(self._payees(owner="Robin"),
                         ["LANTERN GROCERY", "LANTERN GROCERY", "OTTER CAFE"])
        self.assertEqual(self._payees(owner="Nobody"), [])

    def test_spending_only_drops_card_payments_and_income(self):
        got = self._payees(spending_only=True)
        self.assertNotIn("CARD PAYMENT", got)
        self.assertNotIn("ACME PAYROLL", got)
        self.assertIn("OTTER CAFE", got)

    def test_several_accounts_by_their_opaque_ids(self):
        ov = lr.get_overview(self.conn, {})
        ids = {a["name"]: a["id"] for a in ov["accounts"]}
        got = _search(self.conn, account_ids=[ids["Test Checking"]])
        self.assertTrue(got["transactions"])
        self.assertTrue(all(t["account_id"] == ids["Test Checking"]
                            for t in got["transactions"]))
        both = _search(self.conn, account_ids=list(ids.values()))
        self.assertEqual(both["totals"]["count"], 7)

    def test_offset_pages_without_overlap(self):
        a = _search(self.conn, limit=3)["transactions"]
        b = _search(self.conn, limit=3, offset=3)["transactions"]
        self.assertFalse({t["id"] for t in a} & {t["id"] for t in b})


class OverviewTests(Seeded):

    def test_accounts_carry_dates_mask_and_no_stored_id(self):
        ov = lr.get_overview(self.conn, {})
        card = next(a for a in ov["accounts"] if a["name"] == "Test Card")
        self.assertEqual(card["mask"], "7788")
        self.assertEqual(card["owner"], "Robin")
        self.assertEqual(card["first_transaction_date"],
                         (TODAY.replace(day=1) - dt.timedelta(days=1))
                         .replace(day=20).isoformat())
        self.assertEqual(card["last_transaction_date"],
                         TODAY.replace(day=4).isoformat())
        self.assertTrue(card["id"].startswith("acct_"))
        self.assertNotIn("card", {a["id"] for a in ov["accounts"]})
        self.assertEqual(ov["owners"], ["Robin"])
        self.assertEqual(ov["currency"], "USD")

    def test_a_hidden_account_is_listed_marked_and_reachable_by_id(self):
        # the Transactions page leaves a hidden account out of every
        # search unless that account is asked for by name; the overview
        # lists it marked, so the one id a search accepts is one it shows
        self.conn.execute("UPDATE accounts SET user_removed_at = now() "
                          "WHERE id = 'card'")
        accts = {a["name"]: a for a in lr.get_overview(self.conn, {})["accounts"]}
        self.assertIs(accts["Test Card"]["hidden"], True)
        self.assertIs(accts["Test Checking"]["hidden"], False)
        payees = {t["payee"] for t in _search(self.conn)["transactions"]}
        self.assertNotIn("OTTER CAFE", payees)
        only = _search(self.conn, account_ids=[accts["Test Card"]["id"]])
        self.assertIn("OTTER CAFE", {t["payee"] for t in only["transactions"]})


class SafetyTests(Seeded):

    def test_a_long_digit_run_in_a_name_is_cut_to_its_last_four(self):
        add_txn(self.conn, TODAY, 9.0, "PAYMENT 4111222233334444 THANK YOU")
        got = _search(self.conn, query="thank you")
        payee = got["transactions"][0]["payee"]
        self.assertNotIn("4111222233334444", payee)
        self.assertIn("…4444", payee)

    def test_control_and_direction_marks_are_removed(self):
        self.assertEqual(lr.clean_text("A\u202eB\nC\x00D"), "AB CD")
        self.assertEqual(len(lr.clean_text("x" * 500)), lr.TEXT_MAX)

    def test_invisible_characters_cannot_smuggle_a_card_number_past_the_cut(self):
        for shape in ("4111\x014111\u202e1111\x1f1111",
                      "4111\u200b4111\u200c1111\u200d1111",
                      "4111\u20604111\ufeff1111\u200e1111",
                      "PAY 4111\u200f4111\u202a1111\u20661111 OK"):
            out = lr.clean_text(shape)
            digits = "".join(ch for ch in out if ch.isdigit())
            self.assertNotIn("41114111", digits, repr(out))
            self.assertLessEqual(len(digits), 8, repr(out))
            self.assertFalse(any(ch in out for ch in
                                 "\x01\x1f\u200b\u200c\u200d\u200e\u200f"
                                 "\u2060\ufeff\u202a\u202e\u2066"), repr(out))

    def test_statement_text_is_not_returned(self):
        row = _search(self.conn, limit=1)["transactions"][0]
        self.assertNotIn("bank_text", row)


class CoverageAndEmptyTests(Seeded):

    def test_a_period_before_an_account_s_history_says_so(self):
        early = (self.start - dt.timedelta(days=60)).isoformat()
        got = _search(self.conn, start_date=early)
        self.assertIn("coverage_note", got)
        self.assertIn("Test Card", got["coverage_note"])
        inside = _search(self.conn, start_date=TODAY.replace(day=5).isoformat())
        self.assertNotIn("coverage_note", inside)

    def test_nothing_matched_is_a_success_with_zero_totals(self):
        got = _search(self.conn, query="no such merchant anywhere")
        self.assertEqual(got["totals"], {"count": 0, "out": 0.0, "in": 0.0,
                                         "net": 0.0})
        self.assertEqual(got["transactions"], [])
        self.assertFalse(got["has_more"])
        self.assertIn("note", got)


class ArgumentTests(unittest.TestCase):

    def _bad(self, msg, **kw):
        with self.assertRaises(lr.ToolInputError) as cm:
            lr.parse_args("search_transactions", kw)
        self.assertIn(msg, str(cm.exception))

    def test_out_of_range_arguments_are_refused_in_words(self):
        self._bad("start_date must be between", start_date="0001-01-01")
        self._bad("end_date must be between", end_date="9999-12-31")
        self._bad("min_amount must be at most", min_amount=10 ** 400)
        self._bad("max_amount must be at most", max_amount=1e300)
        self._bad("query must not contain control characters", query="a\x00b")
        self._bad("owner must not contain control characters", owner="x\ny")
        self._bad("account_ids must not contain control characters",
                  account_ids=["acct\x00"])

    def test_only_the_plain_date_form_is_taken(self):
        for form in ("20260501", "2026-W18-1", "2026-05-01T00:00", "2026-5-1"):
            self._bad("written YYYY-MM-DD", start_date=form)
        self.assertEqual(lr.parse_args("search_transactions",
                                       {"start_date": "2026-05-01"})["start_date"],
                         dt.date(2026, 5, 1))

    def test_bad_arguments_say_what_is_wrong(self):
        self._bad("limit must be at most 100", limit=101)
        self._bad("unknown argument 'household'", household="x")
        self._bad("start_date must be a date", start_date="last week")
        self._bad("direction must be one of", direction="sideways")
        self._bad("spending_only must be true or false", spending_only="yes")
        self._bad("limit must be an integer", limit=True)

    def test_end_before_start_is_refused_in_words(self):
        conn = make_db()
        self.addCleanup(conn.close)
        with self.assertRaises(lr.ToolInputError) as cm:
            _search(conn, start_date="2026-03-10", end_date="2026-03-01")
        self.assertEqual(str(cm.exception), "end_date is before start_date")

    def test_an_unknown_account_id_is_refused(self):
        conn = make_db()
        self.addCleanup(conn.close)
        with self.assertRaises(lr.ToolInputError):
            _search(conn, account_ids=["acct_nope"])

    def test_the_schema_is_the_declaration(self):
        s = lr.input_schema("search_transactions")
        self.assertFalse(s["additionalProperties"])
        self.assertEqual(set(s["properties"]), set(lr.SPECS["search_transactions"]))
        self.assertEqual(s["properties"]["limit"]["maximum"], 100)


if __name__ == "__main__":
    unittest.main()
