"""Retiring one of two lineages of the same charge moves a person's pin as
a UNIT and leaves nothing stranded on the row that goes.

``override_source`` says who set ``category_override`` — a person, a store's
item match, a bill's stamp — and every writer refuses to overwrite a kind
above its own. A pin that lands on the survivor without its kind reads as
one nobody set, so the next store or bill pass overwrites a person's
answer; and a person's "use automatic" is an EMPTY pin wearing their kind,
so a survivor holding one already has an answer and must not have it filled
in from the row being retired.

What the survivor takes must also be released from the retired row. A
removed row still wearing an override is work every read hides, and the
nightly re-anchor sweep then finds it for the rest of the account's life
and hunts a twin it can never move it onto.
"""
import datetime as dt
import unittest

from oikonome.sync import adopt, reanchor

from .util import make_db


def _item(conn, iid):
    conn.execute("INSERT INTO items (id, aggregator, institution_id, "
                 "institution_name, status, access_token) VALUES "
                 "(%s,'plaid','ins_9','Credit Union','ok','tok')", (iid,))


def _account(conn, aid, iid):
    conn.execute("INSERT INTO accounts (id, item_id, name, type, mask) "
                 "VALUES (%s,%s,'Business Checking','depository','1111')",
                 (aid, iid))


def _txn(conn, tid, aid, date, amount, name, *, merchant=None):
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount, name, "
        "merchant_name, pending, removed) VALUES (%s,%s,%s,%s,%s,%s,0,0)",
        (tid, aid, date, amount, name, merchant))


def _pin(conn, tid, category, source):
    conn.execute("UPDATE transactions SET category_override=%s, "
                 "override_source=%s WHERE id=%s", (category, source, tid))


def _cat_of(conn, tid):
    r = conn.execute("SELECT category_override, override_source "
                     "FROM transactions WHERE id=%s", (tid,)).fetchone()
    return r["category_override"], r["override_source"]


class DedupedTwinOverrideTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "cu-item")
        _account(self.conn, "biz", "cu-item")
        self.d = dt.date(2026, 8, 14)

    def _pair(self, amount=20.00):
        """The seam this tool exists for: one charge on one account under a
        raw descriptor and under the cleaned name. The cleaned row is the
        survivor on its name alone, whatever state either side carries."""
        _txn(self.conn, "raw", "biz", self.d, amount,
             "REF000000000042 NORTHWIND HOSTING.COM NORTHWINDHOST.US",
             merchant="Northwindhost.us")
        _txn(self.conn, "clean", "biz", self.d, amount, "Northwind Hosting",
             merchant="Northwind Hosting")

    def test_a_carried_pin_arrives_with_the_hand_that_set_it(self):
        self._pair()
        _pin(self.conn, "raw", "SOFTWARE", "user")
        out = adopt.dedupe_twins(self.conn, "biz")
        self.assertEqual([(o["kept"], o["removed"]) for o in out],
                         [("clean", "raw")])
        self.assertEqual(_cat_of(self.conn, "clean"), ("SOFTWARE", "user"))
        # and the retired row let go of both halves together
        self.assertEqual(_cat_of(self.conn, "raw"), (None, None))

    def test_a_survivor_reset_to_automatic_stays_reset(self):
        """An empty pin with a person's kind on it is their answer of "no
        category". A bill's stamp carried off the retired twin must not fill
        that emptiness, or the reset un-resets itself."""
        self._pair()
        _pin(self.conn, "raw", "RENT_AND_UTILITIES", "bill")
        _pin(self.conn, "clean", None, "user")
        adopt.dedupe_twins(self.conn, "biz")
        self.assertEqual(_cat_of(self.conn, "clean"), (None, "user"))
        # what the survivor refused stays where it was written rather than
        # being nulled on the way past
        self.assertEqual(_cat_of(self.conn, "raw"),
                         ("RENT_AND_UTILITIES", "bill"))

    def test_a_retired_twin_is_not_left_stranded(self):
        """The re-anchor sweep hunts every removed row still carrying an
        override. A dedup that copies user state to the survivor without
        releasing it from the loser hands that sweep a row it can never
        resolve — the survivor has its own answer now — for every night
        after."""
        self._pair()
        _pin(self.conn, "raw", "SOFTWARE", "user")
        self.conn.execute("UPDATE transactions SET owner_override='shared' "
                          "WHERE id=%s", ("raw",))
        adopt.dedupe_twins(self.conn, "biz")
        self.assertEqual(
            self.conn.execute("SELECT owner_override FROM transactions "
                              "WHERE id='clean'").fetchone()["owner_override"],
            "shared")
        self.assertNotIn("raw", [r["id"] for r in reanchor.stranded(self.conn)])

    def test_a_pin_the_survivor_already_had_is_the_one_that_stays(self):
        """Both rows pinned by hand: the survivor keeps its own answer, and
        the retired row keeps the differing one it alone was given — a dedup
        may not decide a person meant the other category."""
        self._pair()
        _pin(self.conn, "raw", "SOFTWARE", "user")
        _pin(self.conn, "clean", "GENERAL_SERVICES", "user")
        adopt.dedupe_twins(self.conn, "biz")
        self.assertEqual(_cat_of(self.conn, "clean"),
                         ("GENERAL_SERVICES", "user"))
        self.assertEqual(_cat_of(self.conn, "raw"), ("SOFTWARE", "user"))


if __name__ == "__main__":
    unittest.main()
