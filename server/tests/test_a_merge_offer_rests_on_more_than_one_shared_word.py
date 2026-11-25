"""Two merchants whose names share only one ordinary word are not offered
as one business.

"Juniper Pub" and "Juniper Tavern" in one town, both registered as bars,
are two bars that happen to share a word; "Copperleaf Books" and
"Copperleaf Gardens" whose bank lines both begin "COPPERLEAF" are two
shops. The registered line of business and the bank line's stem are
evidence only when the names agree on more than that one word: two
shared words, or a stem that is more than a word both names already
carry (a run-together spelling of the whole name is the bank line, not
a shared word).

An offer stored before the rule — its only evidence such a sentence, its
names sharing one word — is not shown on the Merchants page, not counted
by the alert strip, and not approvable from a tab that listed it. It is
not retired: nothing is deleted, the decision is the read's.
"""
import datetime as dt
import json
import unittest

from oikonome.engine import merchant_merge as mm

from .test_merchant_merge_proposals import CITIES, _cand
from .util import TODAY, add_txn, make_db, write_config


class TheProposerWantsMoreThanOneSharedWord(unittest.TestCase):
    def test_one_shared_word_and_one_line_of_business_is_not_an_offer(self):
        c = [_cand("Juniper Tavern", mcc="5813", city="Fernvale"),
             _cand("Juniper Pub", mcc="5813", city="Fernvale")]
        self.assertEqual(mm.propose(c, CITIES), [])

    def test_a_bank_line_that_is_only_the_shared_word_is_not_an_offer(self):
        c = [_cand("Copperleaf Books", stems=["copperleaf"]),
             _cand("Copperleaf Gardens", stems=["copperleaf"])]
        self.assertEqual(mm.propose(c, CITIES), [])

    def test_two_shared_words_and_one_line_of_business_still_offer(self):
        c = [_cand("Juniper Tavern Room", mcc="5813", city="Fernvale"),
             _cand("Juniper Tavern Patio", mcc="5813", city="Fernvale")]
        self.assertEqual(len(mm.propose(c, CITIES)), 1)

    def test_a_bank_line_longer_than_the_shared_word_still_offers(self):
        c = [_cand("Copperleaf Books", stems=["copperleaf books and"]),
             _cand("Copperleaf Readers", stems=["copperleaf books and"])]
        self.assertEqual(len(mm.propose(c, CITIES)), 1)
        c = [_cand("Copperleaf Books", stems=["copperleafbooks"]),
             _cand("Copperleaf Readers", stems=["copperleafbooks"])]
        self.assertEqual(len(mm.propose(c, CITIES)), 1)

    def test_a_name_signal_is_not_judged_by_its_word_count(self):
        # "the same name with the kind of business added" is name evidence
        c = [_cand("Copperleaf"), _cand("Copperleaf Pharmacy")]
        self.assertEqual(len(mm.propose(c, CITIES)), 1)


def _merchant(conn, name):
    return conn.execute(
        "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
        "RETURNING id::text AS id", (name,)).fetchone()["id"]


def _row(conn, mid, day, amount, raw):
    tid = add_txn(conn, day, amount, raw, merchant=raw.title(),
                  primary="FOOD_AND_DRINK")
    conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                 (mid, tid))


class AStoredOneWordOfferIsNotShown(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.ids = {}
        for name in ("Juniper Tavern", "Juniper Pub",
                     "Ashbury Toywo", "Ashbury Toyworks Market"):
            self.ids[name] = _merchant(self.conn, name)
            for i in range(3):
                _row(self.conn, self.ids[name],
                     TODAY - dt.timedelta(days=200 - i * 30), 31.0, name.upper())

    def _store(self, frm, into, signals):
        pid = mm._sig(self.ids[frm], self.ids[into])
        self.conn.execute(
            "INSERT INTO merchant_merge_proposals (id, from_merchant_id, "
            "into_merchant_id, evidence, status, created_at) "
            "VALUES (%s, %s, %s, %s::jsonb, 'pending', now() - interval '9 days')",
            (pid, self.ids[frm], self.ids[into], json.dumps(
                {"signals": signals, "supports": [],
                 "from": {"name": frm, "rows": 3, "samples": []},
                 "into": {"name": into, "rows": 3, "samples": []}})))
        return pid

    def test_it_is_left_off_the_list_and_the_count_and_cannot_be_approved(self):
        one = self._store("Juniper Pub", "Juniper Tavern",
                          ["same registered line of business (MCC 5813) in Fernvale"])
        self._store("Ashbury Toywo", "Ashbury Toyworks Market",
                    ['"ashbury toywo" looks like "ashbury toyworks market" cut short'])
        shown = [o["from"] for o in mm.pending(self.conn)]
        self.assertEqual(shown, ["Ashbury Toywo"])
        self.assertEqual(mm.notice(self.conn)["pending"], 1)
        out = mm.decide(self.conn, one, "approve")
        self.assertTrue(out.get("conflict"), out)
        # nothing retired, nothing merged
        st = self.conn.execute(
            "SELECT status FROM merchant_merge_proposals WHERE id=%s",
            (one,)).fetchone()["status"]
        self.assertEqual(st, "pending")

    def test_a_stored_one_word_bank_line_offer_is_not_shown(self):
        self._store("Juniper Pub", "Juniper Tavern",
                    ['both carry the bank line "juniper…"'])
        self.assertEqual(mm.pending(self.conn), [])
        self.assertIsNone(mm.notice(self.conn))


if __name__ == "__main__":
    unittest.main()
