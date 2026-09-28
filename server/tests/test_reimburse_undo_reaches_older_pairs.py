"""The Matched list — the only place a wrong reimbursement pair can be
unlinked — must reach every pair, not just the newest page.

A wrong match takes the charge off the pending list and stamps both sides
as transfers, so a pair older than the newest page could never be undone.
The pairs door pages further back (`page`, with `more` saying whether a
further page exists) and finds a pair by either side's merchant (`q`); the
default answer is the same newest page it always was.

The candidates door states each side's REMAINDER (what the anchor still
needs, what already came back to it, what each candidate has left), which
the matchers word their rows and the selection by — the server ranks and
links by those figures, not by face value.
"""
import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import data as d
from oikonome.web import security

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config


class ReimbursePairsPagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"rp-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # the oldest pair is the odd one out by name, so a search finds
            # it however many newer pairs bury it
            day = TODAY - dt.timedelta(days=90)
            e = add_txn(conn, day, 31.0, "INVENTED LANTERN SHOP")
            p = add_txn(conn, day, -31.0, "INVENTED REFUND DESK",
                        account="chk", primary="INCOME")
            d.link_reimbursement(conn, e, p)
            cls.oldest = (e, p)
            for i in range(55):
                e = add_txn(conn, TODAY, 10.0 + i, f"FILLER CHARGE {i}")
                p = add_txn(conn, TODAY, -(10.0 + i), f"FILLER PAYBACK {i}",
                            account="chk", primary="INCOME")
                d.link_reimbursement(conn, e, p)
        finally:
            conn.close()

    def setUp(self):
        security._limiter._hits.clear()

    def _get(self, **params):
        r = self.client.get("/api/reimburse/pairs", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_default_answer_is_the_newest_page(self):
        out = self._get()
        self.assertEqual(len(out["pairs"]), 50)
        self.assertTrue(out["more"])
        ids = {(p["expense_id"], p["reimburse_id"]) for p in out["pairs"]}
        self.assertNotIn(self.oldest, ids)

    def test_the_next_page_reaches_the_oldest_pair_without_repeats(self):
        first = self._get()["pairs"]
        second = self._get(page=2)
        self.assertFalse(second["more"])
        self.assertEqual(len(first) + len(second["pairs"]), 56)
        ids = [(p["expense_id"], p["reimburse_id"])
               for p in first + second["pairs"]]
        self.assertEqual(len(set(ids)), 56)
        self.assertIn(self.oldest, ids)

    def test_a_search_finds_an_old_pair_by_either_side(self):
        for q in ("lantern", "REFUND DESK"):
            out = self._get(q=q)
            self.assertEqual(
                [(p["expense_id"], p["reimburse_id"]) for p in out["pairs"]],
                [self.oldest], q)
            self.assertFalse(out["more"])
        # the typed text is literal, not a pattern
        self.assertEqual(self._get(q="%")["pairs"], [])

    def test_a_nonsense_page_is_not_an_error(self):
        self.assertEqual(len(self._get(page=0)["pairs"]), 50)
        self.assertEqual(self._get(page=99)["pairs"], [])


class CandidatesStateRemaindersTests(unittest.TestCase):
    """A $100 charge that already got $60 back needs $40: a fresh $40
    deposit is its exact match, and the candidates door must say so in
    the figures it hands the clients."""

    def setUp(self):
        from .util import make_db
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_anchor_and_candidates_carry_what_is_left(self):
        charge = add_txn(self.conn, TODAY, 100.0, "INVENTED CLINIC")
        first = add_txn(self.conn, TODAY, -60.0, "INVENTED INSURER",
                        account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, charge, first, partial=True)
        fresh = add_txn(self.conn, TODAY, -40.0, "INVENTED INSURER TWO",
                        account="chk", primary="INCOME")
        # a $150 deposit that already fully repaid a $100 charge has $50 left
        other = add_txn(self.conn, TODAY, 100.0, "INVENTED OTHER CHARGE")
        big = add_txn(self.conn, TODAY, -150.0, "INVENTED BIG CHECK",
                      account="chk", primary="INCOME")
        self.conn.execute("INSERT INTO reimbursements (expense_id,"
                          " reimburse_id) VALUES (%s,%s)", (other, big))
        anchor, cands = d.reimbursement_candidates(self.conn, charge)
        self.assertAlmostEqual(anchor["left_amount"], 40.0, places=2)
        self.assertAlmostEqual(anchor["received"], 60.0, places=2)
        by_id = {c["id"]: c for c in cands}
        self.assertEqual(cands[0]["id"], fresh)
        self.assertAlmostEqual(by_id[fresh]["left_amount"], 40.0, places=2)
        self.assertAlmostEqual(by_id[big]["left_amount"], 50.0, places=2)


if __name__ == "__main__":
    unittest.main()
