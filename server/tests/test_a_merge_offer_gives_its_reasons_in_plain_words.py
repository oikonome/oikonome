"""A merge offer's reasons are written for the person deciding it: a name
"cut short", a category by its own name, a month as people say it.

They read "cut mid-word at 12 characters", "both categorised FOOD AND
DRINK" and "stops 11/26" — a month and year that reads as a day.

A date reason is the ledger's as it is when the offer is read, not as it
was the night the offer was made: a side said to stop in August beside a
box whose last charge is in October contradicts itself, and an offer
stored under the old wording kept it. An offer one of whose merchants
holds no counted row today (its rows withdrawn by a feed, or only on a
hidden account) is not shown or counted, but it is not retired either:
when the rows come back so does the offer. Only a merchant merged away
or deleted retires it. The box counts every row approving moves, not
only charges.
"""
import datetime as dt
import json
import unittest

from oikonome.engine import merchant_merge as mm

from .util import TODAY, add_txn, make_db, write_config


def _side(name, first, last, category="FOOD_AND_DRINK", median=20.0):
    return {"name": name, "first": first, "last": last,
            "category": category, "median": median}


class MergeReasonWordsTests(unittest.TestCase):
    def test_a_cut_name_reads_cut_short(self):
        why = mm._truncation("invented supply", "invented supplyxq")
        self.assertEqual(why, "cut short")
        self.assertNotIn("mid-word", why)

    def test_a_shared_category_is_named_as_the_app_names_it(self):
        out = mm.supports(_side("A", dt.date(2026, 1, 1), dt.date(2026, 3, 1)),
                          _side("B", dt.date(2026, 1, 5), dt.date(2026, 3, 9)))
        self.assertIn("both filed under Food and Drink", out)

    def test_a_hand_over_is_dated_by_month_name(self):
        out = mm.supports(
            _side("Old Name", dt.date(2025, 1, 1), dt.date(2026, 10, 2)),
            _side("New Name", dt.date(2026, 11, 3), dt.date(2026, 11, 9)))
        self.assertIn('"Old Name" stops in Oct 2026, '
                      '"New Name" starts in Nov 2026', out)



def _merchant(conn, name):
    return conn.execute(
        "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
        "RETURNING id::text AS id", (name,)).fetchone()["id"]


def _row(conn, mid, day, amount, raw):
    tid = add_txn(conn, day, amount, raw, merchant=raw.title(),
                  primary="FOOD_AND_DRINK")
    conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                 (mid, tid))
    return tid


class TheReasonsAreReadFromTheLedgerNow(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.old = _merchant(self.conn, "Ashbury Toywo")
        self.new = _merchant(self.conn, "Ashbury Toyworks Market")
        # a clean hand-over: the cut spelling until 200 days ago, the full
        # one after
        for i in range(4):
            _row(self.conn, self.old, TODAY - dt.timedelta(days=300 - i * 20),
                 24.5, "ASHBURY TOYWO")
            _row(self.conn, self.new, TODAY - dt.timedelta(days=150 - i * 20),
                 24.5, "ASHBURY TOYWORKS MARKET")
        mm.run(self.conn)
        (self.offer,) = mm.pending(self.conn)

    def tearDown(self):
        self.conn.close()

    def _hand_over(self, offer):
        return [s for s in offer["supports"] if " stops " in s]

    def test_a_side_that_charged_since_is_not_said_to_stop(self):
        self.assertEqual(len(self._hand_over(self.offer)), 1)
        _row(self.conn, self.old, TODAY - dt.timedelta(days=5), 24.5,
             "ASHBURY TOYWO")
        (o,) = mm.pending(self.conn)
        self.assertEqual(self._hand_over(o), [], o["supports"])
        self.assertIn("both filed under Food and Drink", o["supports"])

    def test_a_stored_reason_in_the_old_wording_is_said_again_in_words(self):
        old = ['"Ashbury Toywo" stops 05/26, '
               '"Ashbury Toyworks Market" starts 06/26',
               "both filed under Food and Drink"]
        self.conn.execute(
            "UPDATE merchant_merge_proposals SET evidence = jsonb_set("
            "evidence, '{supports}', %s::jsonb)", (json.dumps(old),))
        (o,) = mm.pending(self.conn)
        (line,) = self._hand_over(o)
        self.assertIn(" stops in ", line)
        self.assertNotRegex(line, r"\d{2}/\d{2}")

    def _store_supports(self, reasons):
        self.conn.execute(
            "UPDATE merchant_merge_proposals SET evidence = jsonb_set("
            "evidence, '{supports}', %s::jsonb)", (json.dumps(reasons),))

    def test_a_category_the_boxes_no_longer_share_is_not_cited(self):
        # one side's rows were filed elsewhere since the offer was made:
        # the box beside the reason says the new category
        self.conn.execute(
            "UPDATE transactions SET category_override = 'GENERAL_MERCHANDISE'"
            " WHERE merchant_id = %s", (self.old,))
        (o,) = mm.pending(self.conn)
        self.assertFalse(any(s.startswith("both filed under")
                             for s in o["supports"]), o["supports"])

    def test_every_stored_reason_is_said_again_from_the_ledger(self):
        self._store_supports(["both categorised FOOD AND DRINK",
                              "similar charges (typically $3–$5)"])
        (o,) = mm.pending(self.conn)
        self.assertEqual(o["supports"][0], "both filed under Food and Drink")
        self.assertNotIn("both categorised FOOD AND DRINK", o["supports"])
        self.assertIn("similar charges (typically $24–$24)", o["supports"])
        self.assertNotIn("similar charges (typically $3–$5)", o["supports"])

    def test_charges_no_longer_alike_are_not_called_similar(self):
        self._store_supports(["both filed under Food and Drink",
                              "similar charges (typically $24–$24)"])
        for i in range(6):
            _row(self.conn, self.new, TODAY - dt.timedelta(days=40 + i),
                 240.0, "ASHBURY TOYWORKS MARKET")
        (o,) = mm.pending(self.conn)
        self.assertFalse(any(s.startswith("similar charges")
                             for s in o["supports"]), o["supports"])

    def _status(self):
        return self.conn.execute(
            "SELECT status FROM merchant_merge_proposals").fetchone()["status"]

    def test_an_offer_whose_side_empties_is_hidden_and_comes_back(self):
        # a feed withdraws a side's rows and re-posts them: while it holds
        # none the offer is neither shown nor counted, but the nightly in
        # between does not retire it
        self.conn.execute("UPDATE transactions SET removed = 1 "
                          "WHERE merchant_id = %s", (self.old,))
        self.assertEqual(mm.pending(self.conn), [])
        self.assertEqual((mm.notice(self.conn) or {}).get("pending", 0), 0)
        self.assertEqual(mm.run(self.conn)["stale"], 0)
        self.assertEqual(self._status(), "pending")
        self.conn.execute("UPDATE transactions SET removed = 0 "
                          "WHERE merchant_id = %s", (self.old,))
        (o,) = mm.pending(self.conn)
        self.assertEqual(o["id"], self.offer["id"])
        self.assertEqual(len(self._hand_over(o)), 1, o["supports"])

    def test_an_offer_on_a_hidden_accounts_rows_waits_for_a_counted_row(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,user_removed_at) VALUES ('hid','it1','Hidden',"
            "'credit','credit card',0,now())")
        self.conn.execute("UPDATE transactions SET account_id = 'hid' "
                          "WHERE merchant_id = %s", (self.old,))
        self.assertEqual(mm.pending(self.conn), [])
        mm.run(self.conn)
        self.assertEqual(self._status(), "pending")
        _row(self.conn, self.old, TODAY - dt.timedelta(days=260), 24.5,
             "ASHBURY TOYWO")
        (o,) = mm.pending(self.conn)
        self.assertEqual(o["id"], self.offer["id"])

    def test_an_offer_left_off_the_list_is_not_approved_from_a_stale_tab(self):
        # the tab listed the offer before a side's rows were withdrawn:
        # approving it then would merge a side nobody can see
        self.conn.execute("UPDATE transactions SET removed = 1 "
                          "WHERE merchant_id = %s", (self.old,))
        r = mm.decide(self.conn, self.offer["id"], "approve",
                      shown_into=self.offer["into_id"])
        self.assertTrue(r.get("conflict"), r)
        self.assertEqual(self._status(), "pending")
        self.assertIsNone(self.conn.execute(
            "SELECT merged_into FROM merchants WHERE id = %s",
            (self.old,)).fetchone()["merged_into"])

    def test_an_offer_whose_merchant_was_merged_away_is_retired(self):
        self.conn.execute("UPDATE merchants SET merged_into = %s "
                          "WHERE id = %s", (self.new, self.old))
        self.assertEqual(mm.run(self.conn)["stale"], 1)
        self.assertEqual(self._status(), "stale")

    def test_the_box_counts_every_row_approving_moves(self):
        # a refund on the folded side moves with it
        _row(self.conn, self.old, TODAY - dt.timedelta(days=250), -10.0,
             "ASHBURY TOYWO")
        (o,) = mm.pending(self.conn)
        side = (o["from_facts"] if o["from_id"] == self.old
                else o["into_facts"])
        self.assertEqual(side["rows"], 5)
        self.assertEqual(side["total"], 88.0)
        self.assertEqual(o["merged"]["rows"], 9)


if __name__ == "__main__":
    unittest.main()
