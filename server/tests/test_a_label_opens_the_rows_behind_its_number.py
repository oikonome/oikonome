"""Every number a household can read has to be openable, and what opens
must be the rows that number counted — not a near-miss.

A bucket tile (Food, Everything else, a carve-out) is a sum the budget
engine alone can reproduce: bill-shaped rows are out, Costco/Amazon food
overrides are in, envelope overflow has been split off its parent
purchase, and a carve-out's merchant rules beat its category rules. So a
tile's link names the BUCKET and the server asks the engine which rows
those were. A category label on a lens is the other case — that one is a
stored value, so it links by category — but it still has to arrive with
the month it was read in, or it opens a lifetime of history.
"""

import datetime as dt
import os
import re
import unittest
import uuid
import zoneinfo
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.web import lenses

from .util import TODAY, _ensure_db, add_bill, add_txn, seed_accounts, write_config

# a fixed household moment so the endpoint's "this month" is the fixture's
# month on any day the suite runs
LOCAL_NOW = dt.datetime(TODAY.year, TODAY.month, TODAY.day, 12, 0,
                        tzinfo=zoneinfo.ZoneInfo("UTC"))

CUSTOM_BUCKETS = [
    # a carve-out claimed by MERCHANT — no category rule at all, which is
    # exactly why "cat=Pets" could never have listed it
    {"name": "Pets", "parent": "other", "monthly": 100,
     "merchants": ["Chewy"]},
    # and one claimed by CATEGORY, under a plan label no row stores
    {"name": "Kids", "parent": "other", "monthly": 50,
     "categories": ["GENERAL_SERVICES"]},
]


def seed_month(conn) -> dict:
    """The fixture month: two food rows (one of them a Costco override),
    one bill-shaped food row, one plain other row, and one row for each
    carve-out — plus a previous month, so month scoping has something to
    leave out."""
    write_config(conn, food_monthly=1000, other_monthly=1000,
                 custom_buckets=CUSTOM_BUCKETS, today_view="detail")
    add_bill(conn, "Blue Apron", 150.0, next_due=dt.date(2026, 7, 1),
             last_seen=dt.date(2026, 7, 1))
    ids = {
        "safeway": add_txn(conn, dt.date(2026, 7, 10), 40.0, "SAFEWAY",
                           primary="FOOD_AND_DRINK"),
        # Food by OVERRIDE, not by category: the tile counts it, and
        # cat=FOOD_AND_DRINK never would
        "costco": add_txn(conn, dt.date(2026, 7, 11), 120.0, "COSTCO WHSE",
                          primary="GENERAL_MERCHANDISE",
                          override="Costco - Food & Drink"),
        # bill-shaped: matched to its occurrence, so it is FIXED spend and
        # no part of the Food tile
        "apron": add_txn(conn, dt.date(2026, 7, 1), 150.0, "BLUE APRON",
                         primary="FOOD_AND_DRINK"),
        "gadget": add_txn(conn, dt.date(2026, 7, 12), 80.0, "ELECTRONICS STORE",
                          primary="GENERAL_MERCHANDISE"),
        "chewy": add_txn(conn, dt.date(2026, 7, 13), 60.0, "CHEWY COM",
                         primary="GENERAL_MERCHANDISE"),
        "tots": add_txn(conn, dt.date(2026, 7, 14), 30.0, "TINY TOTS",
                        primary="GENERAL_SERVICES"),
        "june_food": add_txn(conn, dt.date(2026, 6, 20), 500.0, "SAFEWAY",
                             primary="FOOD_AND_DRINK"),
        "june_blank": add_txn(conn, dt.date(2026, 6, 21), 25.0, "MYSTERY",
                              primary=None),
    }
    return ids


class BucketLedgerEngineTests(unittest.TestCase):
    """budget.bucket_ledger — the row set behind one tile."""

    def setUp(self):
        from .util import make_db
        self.conn = make_db()
        self.ids = seed_month(self.conn)
        self.st = budget.month_status(self.conn, TODAY)

    def tearDown(self):
        self.conn.close()

    def test_food_bucket_is_the_tiles_rows_overrides_in_bills_out(self):
        info = budget.bucket_ledger(self.st, "food")
        self.assertEqual(info["label"], "Food")
        self.assertEqual(set(info["txn_ids"]),
                         {self.ids["safeway"], self.ids["costco"]})
        self.assertEqual(info["amount"],
                         round(self.st["buckets"]["food"]["actual"], 2))
        self.assertEqual(info["amount"], 160.0)

    def test_parent_bucket_drops_what_its_carve_outs_claimed(self):
        """The Everything-else tile shows its DISPLAY number once carve-outs
        exist, so its rows are its own — a dollar must not be listed under
        two tiles that are shown side by side."""
        other = budget.bucket_ledger(self.st, "other")
        self.assertEqual(other["label"], "Everything else")
        self.assertEqual(set(other["txn_ids"]), {self.ids["gadget"]})
        self.assertEqual(other["amount"],
                         round(self.st["buckets"]["other"]["display_actual"], 2))
        pets = budget.bucket_ledger(self.st, "Pets")
        self.assertEqual(set(pets["txn_ids"]), {self.ids["chewy"]})
        self.assertEqual(pets["amount"], 60.0)
        kids = budget.bucket_ledger(self.st, "Kids")
        self.assertEqual(set(kids["txn_ids"]), {self.ids["tots"]})
        self.assertEqual(kids["amount"], 30.0)

    def test_a_name_that_is_not_a_bucket_has_no_rows(self):
        self.assertIsNone(budget.bucket_ledger(self.st, "Nope"))
        self.assertIsNone(budget.bucket_ledger(self.st, "fixed"))

    def test_envelope_overflow_opens_the_purchase_it_came_from(self):
        """An envelope's overflow slice is not a ledger row — it is part of
        one. The tile counts the slice; the listing has to open the
        transaction, because that is what a ledger holds."""
        write_config(self.conn, food_monthly=1000, other_monthly=1000)
        tid = add_txn(self.conn, dt.date(2026, 7, 9), 200.0, "GAS AND GO",
                      primary="TRANSPORTATION")
        add_bill(self.conn, "Gas and Go", 50.0, bill_type="envelope",
                 last_seen=dt.date(2026, 7, 1))
        st = budget.month_status(self.conn, TODAY)
        overflow = st["buckets"]["other"]["rows"]
        self.assertTrue(any(r["category"] == "envelope overflow"
                            for r in overflow), "fixture spent past the cap")
        info = budget.bucket_ledger(st, "other")
        self.assertIn(tid, info["txn_ids"])
        self.assertEqual(len(info["txn_ids"]), len(set(info["txn_ids"])),
                         "the purchase is listed once, not twice")


class LedgerLinkApiTests(unittest.TestCase):
    """GET /api/transactions — the doors the labels open."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        r = cls.client.post("/api/signup", data={
            "email": f"links-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            cls.ids = seed_month(conn)
        finally:
            conn.close()

    def get(self, **params):
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL_NOW):
            r = self.client.get("/api/transactions", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_bucket_listing_is_the_tiles_own_rows_and_total(self):
        body = self.get(bucket="food", y=2026, m=7)
        self.assertEqual(body["mode"], "search")
        self.assertEqual(body["bucket"], "food")
        self.assertEqual(body["bucket_label"], "Food")
        self.assertEqual({r["id"] for r in body["rows"]},
                         {self.ids["safeway"], self.ids["costco"]})
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["amount_sum"], 160.0)

    def test_every_bucket_shape_opens_its_own_rows(self):
        other = self.get(bucket="other", y=2026, m=7)
        self.assertEqual(other["bucket_label"], "Everything else")
        self.assertEqual({r["id"] for r in other["rows"]},
                         {self.ids["gadget"]})
        self.assertEqual(other["amount_sum"], 80.0)
        pets = self.get(bucket="Pets", y=2026, m=7)
        self.assertEqual(pets["bucket_label"], "Pets")
        self.assertEqual({r["id"] for r in pets["rows"]}, {self.ids["chewy"]})
        kids = self.get(bucket="Kids", y=2026, m=7)
        self.assertEqual(kids["bucket_label"], "Kids")
        self.assertEqual({r["id"] for r in kids["rows"]}, {self.ids["tots"]})

    def test_a_past_months_bucket_lists_that_month(self):
        body = self.get(bucket="food", y=2026, m=6)
        self.assertEqual({r["id"] for r in body["rows"]},
                         {self.ids["june_food"]})
        self.assertEqual(body["amount_sum"], 500.0)

    def test_a_past_days_bucket_stops_on_that_day(self):
        """A Today page read on an earlier day of the month shows that day's
        tiles; the rows its label opens must be the ones counted by then,
        not the month so far."""
        body = self.get(bucket="food", y=2026, m=7, as_of="2026-07-10")
        self.assertEqual({r["id"] for r in body["rows"]},
                         {self.ids["safeway"]})
        self.assertEqual(body["amount_sum"], 40.0)

    def test_an_as_of_outside_its_month_or_after_today_is_refused(self):
        for bad in ("2026-06-30", "2026-07-20", "not-a-day"):
            with mock.patch("oikonome.localtime.now_local",
                            return_value=LOCAL_NOW):
                r = self.client.get("/api/transactions", params={
                    "bucket": "food", "y": 2026, "m": 7, "as_of": bad})
            self.assertEqual(r.status_code, 400, bad)

    def test_a_bucket_with_nothing_in_it_is_an_empty_list(self):
        """A month the carve-out never spent in, and a month that has not
        happened yet, are empty listings — the honest answer, not a 500
        from an id filter with nothing in it."""
        for y, m in ((2026, 6), (2026, 12), (2026, 1)):
            body = self.get(bucket="Kids", y=y, m=m)
            self.assertEqual(body["rows"], [], f"{y}-{m}")
            self.assertEqual(body["total"], 0)
            self.assertEqual(body["bucket_label"], "Kids")

    def test_a_name_that_is_no_bucket_is_refused_not_guessed(self):
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL_NOW):
            r = self.client.get("/api/transactions",
                                params={"bucket": "Ponies", "y": 2026, "m": 7})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_category_link_carrying_a_month_is_scoped_to_it(self):
        """y/m beside a category must scope the listing; otherwise a July
        Food label opens Food's whole history."""
        body = self.get(cat="FOOD_AND_DRINK", y=2026, m=7)
        got = {r["id"] for r in body["rows"]}
        self.assertIn(self.ids["safeway"], got)
        self.assertNotIn(self.ids["june_food"], got,
                         "another month's rows leaked into a month link")
        june = self.get(cat="FOOD_AND_DRINK", y=2026, m=6)
        self.assertEqual({r["id"] for r in june["rows"]},
                         {self.ids["june_food"]})

    def test_an_explicit_date_range_still_wins_over_the_month(self):
        body = self.get(cat="FOOD_AND_DRINK", y=2026, m=7,
                        date_from="2026-06-01", date_to="2026-06-30")
        self.assertEqual({r["id"] for r in body["rows"]},
                         {self.ids["june_food"]})

    def test_a_plain_search_is_not_narrowed_to_a_month_nobody_asked_for(self):
        """The month defaults for the BROWSE mode; a search with no y/m is
        still the whole history."""
        body = self.get(q="SAFEWAY")
        self.assertEqual({r["id"] for r in body["rows"]},
                         {self.ids["safeway"], self.ids["june_food"]})

    def test_uncategorized_is_a_filter_of_its_own(self):
        """'?' is what the pages show for a row with no category; no row
        stores it, so equality matched nothing and the label looked dead."""
        body = self.get(cat="?", y=2026, m=6)
        self.assertEqual({r["id"] for r in body["rows"]},
                         {self.ids["june_blank"]})

    def test_a_lens_category_key_round_trips_through_the_endpoint(self):
        """The lens renders "GENERAL MERCHANDISE"; the ledger compares
        "GENERAL_MERCHANDISE". The entry carries the key so the client
        never has to guess the spelling back."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            summary = lenses.month_summary(conn, 2026, 7, today=TODAY)
        finally:
            conn.close()
        entry = next(e for e in summary["by_category"]
                     if e[0] == "GENERAL MERCHANDISE")
        self.assertEqual(entry[2], "GENERAL_MERCHANDISE")
        body = self.get(cat=entry[2], y=2026, m=7)
        self.assertTrue(body["rows"], "the key opened nothing")
        self.assertEqual(body["amount_sum"], entry[1])

    def test_a_bucket_listing_stands_down_every_other_lens(self):
        """A bucket is the engine's own row set. A scope or the
        reimbursement lens riding along would empty the listing under a
        chip that still shows the tile's number, so they are ignored."""
        plain = self.get(bucket="food", y=2026, m=7)
        scoped = self.get(bucket="food", y=2026, m=7, scope="business", reimb=1)
        self.assertTrue(plain["rows"])
        self.assertEqual([r["id"] for r in scoped["rows"]],
                         [r["id"] for r in plain["rows"]])
        self.assertEqual(scoped["amount_sum"], plain["amount_sum"])

    def test_the_year_lens_key_is_the_spelling_this_year_stores(self):
        """One label, two stored spellings across the years: the link must
        carry the one the year on view stores, or it opens last year's
        rows under this year's number — or nothing at all."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            add_txn(conn, dt.date(2025, 7, 8), 30.0, "VET", override="Pet_Care")
            add_txn(conn, dt.date(2026, 7, 9), 45.0, "VET", override="Pet Care")
            summary = lenses.year_summary(conn, 2026, today=TODAY)
        finally:
            conn.close()
        entry = next(e for e in summary["categories"] if e[0] == "Pet Care")
        self.assertEqual(entry[3], "Pet Care")
        body = self.get(cat=entry[3], date_from="2026-01-01", date_to="2026-12-31")
        self.assertEqual(len(body["rows"]), 1)
        self.assertEqual(body["amount_sum"], entry[1])

    def test_a_counted_month_category_lists_what_its_label_summed(self):
        """A month lens category is personal spend. A refund filed under
        the same category is not part of that number, so the listing the
        label opens must leave it out and total the label."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            buy = add_txn(conn, dt.date(2026, 5, 10), 90.0, "HARDWARE STORE",
                          primary="HOME_IMPROVEMENT")
            add_txn(conn, dt.date(2026, 5, 12), -30.0, "HARDWARE STORE",
                    primary="HOME_IMPROVEMENT")
            summary = lenses.month_summary(conn, 2026, 5, today=TODAY)
        finally:
            conn.close()
        entry = next(e for e in summary["by_category"]
                     if e[2] == "HOME_IMPROVEMENT")
        body = self.get(cat="HOME_IMPROVEMENT", y=2026, m=5, counted=1)
        self.assertEqual({r["id"] for r in body["rows"]}, {buy})
        self.assertEqual(body["amount_sum"], entry[1])

    def test_a_counted_year_category_lists_what_its_label_summed(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            buy = add_txn(conn, dt.date(2026, 4, 10), 70.0, "GREENLEAF",
                          primary="GARDEN_SUPPLIES")
            add_txn(conn, dt.date(2026, 4, 11), -20.0, "GREENLEAF",
                    primary="GARDEN_SUPPLIES")
            summary = lenses.year_summary(conn, 2026, today=TODAY)
        finally:
            conn.close()
        entry = next(e for e in summary["categories"]
                     if e[3] == "GARDEN_SUPPLIES")
        body = self.get(cat="GARDEN_SUPPLIES", date_from="2026-01-01",
                        date_to="2026-12-31", counted=1)
        self.assertEqual({r["id"] for r in body["rows"]}, {buy})
        self.assertEqual(body["amount_sum"], entry[1])

    def test_a_counted_category_is_one_month_or_one_whole_year(self):
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL_NOW):
            r = self.client.get("/api/transactions", params={
                "cat": "FOOD_AND_DRINK", "counted": 1,
                "date_from": "2026-03-01", "date_to": "2026-03-31"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_uncategorized_and_clustered_lens_entries_say_what_they_are(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            add_txn(conn, dt.date(2026, 7, 8), 15.0, "AMZN MKTP",
                    override="Amazon - Household")
            add_txn(conn, dt.date(2026, 7, 7), 11.0, "WHO KNOWS", primary=None)
            summary = lenses.month_summary(conn, 2026, 7, today=TODAY)
        finally:
            conn.close()
        keys = {e[0]: e[2] for e in summary["by_category"]}
        # the clustered parent is several stored categories added up — no
        # single filter lists it
        self.assertIsNone(keys["Amazon"])
        self.assertEqual(keys["?"], "?")
        subs = {s[0]: s[2] for s in summary["amazon_subs"]}
        self.assertEqual(subs["Household"], "Amazon - Household")


class EmailLabelLinkTests(unittest.TestCase):
    """The daily email's labels open the same doors the page's do — or,
    with no mailable base, they are plain text rather than a dead LAN link."""

    def setUp(self):
        from .util import make_db
        from oikonome.web import report
        self.report = report
        self.conn = make_db()
        self.ids = seed_month(self.conn)
        self.d = report.gather(self.conn, TODAY)

    def tearDown(self):
        self.conn.close()

    def _html(self, base: str | None):
        env = {"OIKONOME_BASE_URL": base} if base else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if not base:
                os.environ.pop("OIKONOME_BASE_URL", None)
            _, _, html = self.report.build(self.d)
        return html

    def test_bucket_links_ride_out_when_the_base_is_mailable(self):
        html = self._html("https://budget.example.test")
        for want in ("/app/transactions?bucket=food&amp;y=2026&amp;m=7&amp;as_of=2026-07-15",
                     "/app/transactions?bucket=other&amp;y=2026&amp;m=7&amp;as_of=2026-07-15",
                     "/app/transactions?bucket=Pets&amp;y=2026&amp;m=7&amp;as_of=2026-07-15"):
            self.assertIn("https://budget.example.test" + want, html, want)
        # no stale category door survives beside them
        self.assertNotIn("transactions?cat=", html)

    def test_the_ampersand_is_escaped_once(self):
        """Jinja autoescape owns the href; a hand-escaped link would arrive
        as &amp;amp; and the month would be lost on the way in."""
        html = self._html("https://budget.example.test")
        self.assertNotIn("&amp;amp;", html)
        hrefs = re.findall(r'href="([^"]*transactions\?bucket=[^"]*)"', html)
        self.assertTrue(hrefs, "no bucket link rendered")
        for href in hrefs:
            self.assertEqual(href.count("&amp;"), 3, href)
            self.assertNotIn("&y=", href)

    def test_without_a_mailable_base_the_labels_are_just_words(self):
        """A relative href in mail resolves against the mail client, which
        is nowhere — so the label ships as plain text instead."""
        html = self._html(None)
        self.assertNotIn("transactions?bucket=", html)
        self.assertNotIn('href="/app', html)
        # and the labels themselves are still there
        for label in ("Food", "Everything else", "Pets"):
            self.assertIn(label, html)


if __name__ == "__main__":
    unittest.main()
