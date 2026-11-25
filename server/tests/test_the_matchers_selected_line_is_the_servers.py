"""The reimbursement matcher's "selected $X of $Y — …" line is the
server's, worked out by the link rule itself on the ticked rows.

Both clients used to add up the ticked rows' remainders and replay the
link rule in their own code, so the line beside Pair was a third copy of
the rule. The server now answers the selection door by running the link
it would make and undoing it: the selected total, what the charge still
needs, and what of each side stays as spend or income. Nothing is
linked by asking. The web prints the line as sent; the phone does too,
and keeps its own sum only for a server without the door.
"""
import os
import pathlib
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import security

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _SelectionDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"rs-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def setUp(self):
        security._limiter._hits.clear()

    def _add(self, amount, name, **kw):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return add_txn(conn, TODAY, amount, name, **kw)
        finally:
            conn.close()

    def _links(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM reimbursements").fetchone()["n"]
        finally:
            conn.close()

    def _ask(self, txn, ids, partial=False):
        r = self.client.post("/api/reimburse/selection", json={
            "txn_id": txn, "other_ids": ids, "partial": partial})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TheSelectionDoorStatesTheLine(_SelectionDoor):

    def test_two_short_deposits_leave_the_rest_as_spend(self):
        charge = self._add(120.0, "INVENTED CLINIC", primary="MEDICAL")
        a = self._add(-30.0, "INVENTED PLAN REFUND", account="chk")
        b = self._add(-50.0, "INVENTED PLAN REFUND", account="chk")
        before = self._links()
        out = self._ask(charge, [a, b])
        self.assertEqual(out["selected"], 80.0)
        self.assertEqual(out["need"], 120.0)
        self.assertEqual(out["line"],
                         "selected $80.00 of $120.00 — $40.00 stays as spend")
        # asking links nothing
        self.assertEqual(self._links(), before)

    def test_one_exact_deposit_reads_exact(self):
        charge = self._add(64.0, "INVENTED OPTICIAN", primary="MEDICAL")
        dep = self._add(-64.0, "INVENTED OPTICIAN REFUND", account="chk")
        self.assertEqual(self._ask(charge, [dep])["line"],
                         "selected $64.00 of $64.00 — exact ✓")

    def test_a_deposit_bigger_than_the_charge_leaves_the_rest_as_income(self):
        charge = self._add(40.0, "INVENTED PHARMACY", primary="MEDICAL")
        dep = self._add(-70.0, "INVENTED CLAIM", account="chk")
        self.assertEqual(
            self._ask(charge, [dep])["line"],
            "selected $70.00 of $40.00 — $30.00 over the charge; "
            "the rest stays income")

    def test_a_deposit_anchor_names_what_stays_income(self):
        dep = self._add(-90.0, "INVENTED EMPLOYER EXPENSES", account="chk")
        c = self._add(25.0, "INVENTED TAXI", primary="TRANSPORTATION")
        self.assertEqual(self._ask(dep, [c])["line"],
                         "selected $25.00 of $90.00 — $65.00 of this deposit "
                         "stays income")


class TheSelectionDoorIsBounded(_SelectionDoor):
    """The door runs the real link path under the household's pairing
    lock and undoes it, so a selection past what a person ticks by hand
    is answered without a line and without that work, and one member
    cannot hold the lock by looping it from many addresses."""

    def _no_lock(self):
        from unittest import mock
        from oikonome.sync import reanchor
        return mock.patch.object(reanchor, "hold_pairing_lock_for_request",
                                 side_effect=AssertionError("lock taken"))

    def test_a_selection_past_the_cap_answers_without_a_line(self):
        from oikonome.web import api_reimburse
        charge = self._add(15.0, "INVENTED BAKERY", primary="FOOD_AND_DRINK")
        ids = [f"invented-{i}" for i in range(api_reimburse.SELECTION_MAX + 1)]
        with self._no_lock():
            out = self._ask(charge, ids)
        self.assertIsNone(out["line"])

    def test_a_selection_at_the_cap_still_gets_its_line(self):
        from oikonome.web import api_reimburse
        charge = self._add(18.0, "INVENTED FLORIST", primary="MEDICAL")
        dep = self._add(-18.0, "INVENTED FLORIST REFUND", account="chk")
        ids = [dep] + [f"invented-{i}"
                       for i in range(api_reimburse.SELECTION_MAX - 1)]
        self.assertEqual(self._ask(charge, ids)["line"],
                         "selected $18.00 of $18.00 — exact ✓")

    def test_the_per_user_limit_holds_across_addresses(self):
        from unittest import mock
        from oikonome.web import api_reimburse
        charge = self._add(22.0, "INVENTED TAILOR", primary="MEDICAL")
        dep = self._add(-22.0, "INVENTED TAILOR REFUND", account="chk")
        seen = iter(f"198.51.100.{i}" for i in range(1, 200))
        with mock.patch.object(security, "client_ip",
                               lambda request: next(seen)), \
             mock.patch.object(api_reimburse, "SELECTION_PER_USER",
                               (3, 3600)):
            codes = [self.client.post("/api/reimburse/selection", json={
                "txn_id": charge, "other_ids": [dep]}).status_code
                for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])


class BothClientsPrintIt(unittest.TestCase):
    web = (ROOT / "webapp/src/pages/Reimburse.tsx").read_text()
    phone = (ROOT / "mobile/src/app/reimburse.tsx").read_text()

    def test_the_web_adds_nothing_up(self):
        self.assertNotIn("reduce((s, a) => s + a", self.web)
        self.assertNotIn("pairGapHint(", self.web)
        self.assertIn("selection.data", self.web)

    def test_both_wait_for_the_ticking_to_settle(self):
        # a burst of ticks is one request, not one per tick
        self.assertIn("setPickedSettled", self.web)
        self.assertIn("setChosenSettled", self.phone)

    def test_the_phone_prints_the_line_when_sent(self):
        self.assertIn("sel.data?.line !== undefined", self.phone)
        self.assertIn("older-server path: stands in for `selection.line`",
                      self.phone)


if __name__ == "__main__":
    unittest.main()
