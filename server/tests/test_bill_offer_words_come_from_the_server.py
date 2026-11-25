"""A bill offer's words are written once, on the server, for both clients.

The web composed "weekly ×2" out of the frequency and interval (read as
twice a week; the server says "every 2 weeks"), the evidence said "on a
?-day cycle" when no cycle was measured, the kind and the advisory tag sat
side by side as a command ("add habit"), and an envelope removal read
"varies month to month" on the web and "one-time" on the phone. The web
and the phone now print `change_text`, `tag_text` and `cadence_text`.
"""

import datetime as dt
import unittest

from oikonome.engine import bills
from oikonome.web.pages import _proposal_summary

from .util import add_bill, make_db, write_config


def _p(**over):
    p = {"kind": "add", "bill_type": "occurrence", "frequency": "WEEKLY",
         "interval": 2, "llm_tag": None, "evidence": {}}
    p.update(over)
    return p


class OfferWordsTests(unittest.TestCase):
    def test_every_two_weeks_is_said_as_such(self):
        w = bills.offer_words(_p(), {})
        self.assertEqual(w["cadence_text"], "every 2 weeks")
        self.assertEqual(w["change_text"], "new bill")

    def test_an_envelope_offer_varies_and_a_removal_has_no_cadence(self):
        self.assertEqual(bills.offer_words(
            _p(bill_type="envelope", frequency=None), {})["cadence_text"],
            "varies month to month")
        w = bills.offer_words(_p(kind="remove", bill_type="envelope",
                                 frequency=None), {})
        self.assertIsNone(w["cadence_text"])
        self.assertEqual(w["change_text"], "stop tracking")
        self.assertIsNone(bills.offer_words(
            _p(kind="merchant", frequency=None), {})["cadence_text"])

    def test_the_tag_is_plain_words_beside_the_change(self):
        self.assertEqual(bills.offer_words(_p(llm_tag="habit"), {})["tag_text"],
                         "looks like everyday spending, not a bill")
        self.assertEqual(
            bills.offer_words(_p(llm_tag="one-off streak"), {})["tag_text"],
            "looks like a few one-off charges, not a bill")
        self.assertEqual(bills.offer_words(_p(), {"income": True})["change_text"],
                         "new income")

    def test_no_measured_cycle_is_no_question_mark(self):
        s = _proposal_summary({"evidence": {"hits": 7}})
        self.assertEqual(s, "7 charges")
        self.assertNotIn("?", s)
        self.assertEqual(_proposal_summary({"evidence": {"hits": 4,
                                                         "cycle_days": 14}}),
                         "4 charges about every 14 days")


class BillsReplyCarriesTheWordsTests(unittest.TestCase):
    def test_the_bills_reply_sends_the_words(self):
        from oikonome.web import api_bills
        conn = make_db()
        write_config(conn)
        self.addCleanup(conn.close)
        add_bill(conn, "Pine Ridge Clinic", 120.0)
        bills._insert_proposal(
            conn, pid="prop:w1", kind="add", payee="Lantern Cleaners",
            amount=80.0, frequency="WEEKLY", interval=2,
            next_due=dt.date.today() + dt.timedelta(days=3),
            evidence={"hits": 5, "cycle_days": 14})
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        out = api_bills.bills_list(user={"tenant_id": tid, "role": "owner",
                                         "id": None}, month=None)
        p = next(x for x in out["proposals"] if x["id"] == "prop:w1")
        self.assertEqual((p["change_text"], p["cadence_text"], p["tag_text"]),
                         ("new bill", "every 2 weeks", None))


if __name__ == "__main__":
    unittest.main()
