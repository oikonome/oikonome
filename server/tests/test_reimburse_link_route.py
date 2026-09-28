"""The reimbursement link door, as the clients use it.

Both clients anchor a link on the charge. A full link from a charge's page
to a deposit much bigger than the charge is linked as partial, and the
response says so in `notes` — not in `errors`, which stay for pairs that
were refused. The activity log records what was linked, not what was
asked: nothing when every pair was refused.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import security

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config


class ReimburseLinkRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"rr-{uuid.uuid4().hex[:8]}@example.dev",
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

    def _linked_rows(self, target):
        r = self.client.get("/api/activity",
                            params={"kind": "reimbursement", "target": target})
        self.assertEqual(r.status_code, 200, r.text)
        return [x for x in r.json()["rows"] if x.get("action") == "linked"]

    def test_charge_anchored_full_link_on_a_bigger_deposit_links_partial(self):
        charge = self._add(60.0, "CLINIC COPAY", primary="MEDICAL")
        check = self._add(-405.0, "INSURANCE CLAIM PAYMENT", account="chk",
                          primary="INCOME")
        r = self.client.post("/api/reimburse/link",
                             json={"txn_id": charge, "other_ids": [check]})
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["linked"], 1)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out.get("partial"), 1)
        self.assertTrue(out.get("notes"))
        rows = self._linked_rows(charge)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["detail"]["count"], 1)
        self.assertTrue(rows[0]["detail"]["partial"])

    def test_a_request_that_links_nothing_is_not_logged(self):
        a = self._add(-20.0, "DEPOSIT A", account="chk", primary="INCOME")
        b = self._add(-30.0, "DEPOSIT B", account="chk", primary="INCOME")
        r = self.client.post("/api/reimburse/link",
                             json={"txn_id": a, "other_ids": [b, "nope"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["linked"], 0)
        self.assertEqual(len(r.json()["errors"]), 2)
        self.assertEqual(self._linked_rows(a), [])

    def test_logged_count_is_what_was_linked(self):
        charge = self._add(50.0, "PHARMACY", primary="MEDICAL")
        dep = self._add(-50.0, "INSURANCE REFUND", account="chk",
                        primary="INCOME")
        same = self._add(-10.0, "ANOTHER DEPOSIT", account="chk",
                         primary="INCOME")
        r = self.client.post("/api/reimburse/link",
                             json={"txn_id": dep, "other_ids": [charge, same,
                                                                "nope"]})
        self.assertEqual(r.json()["linked"], 1)
        rows = self._linked_rows(dep)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["detail"]["count"], 1)

    def test_an_unlink_that_removes_nothing_is_not_logged(self):
        """A second tap, the other device or a retry finds the pair gone:
        it answers without recording another unlink."""
        charge = self._add(35.0, "PHARMACY", primary="MEDICAL")
        dep = self._add(-35.0, "INSURANCE REFUND", account="chk",
                        primary="INCOME")
        r = self.client.post("/api/reimburse/link",
                             json={"txn_id": charge, "other_ids": [dep]})
        self.assertEqual(r.json()["linked"], 1)
        body = {"expense_id": charge, "reimburse_id": dep}
        for _ in range(2):
            r = self.client.post("/api/reimburse/unlink", json=body)
            self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get("/api/activity", params={
            "kind": "reimbursement", "target": charge})
        unlinked = [x for x in r.json()["rows"]
                    if x.get("action") == "unlinked"]
        self.assertEqual(len(unlinked), 1)

    def test_flagging_a_repaid_charge_is_refused(self):
        charge = self._add(45.0, "OPTICIAN", primary="MEDICAL")
        dep = self._add(-45.0, "INSURANCE REFUND", account="chk",
                        primary="INCOME")
        self.client.post("/api/reimburse/link",
                         json={"txn_id": charge, "other_ids": [dep]})
        r = self.client.post(f"/api/reimburse/{charge}/flag",
                             json={"partial": True})
        self.assertEqual(r.status_code, 409, r.text)
        pend = self.client.get("/api/reimburse/pending").json()["pending"]
        self.assertNotIn(charge, [p["id"] for p in pend])

    def _actions(self, target, action):
        r = self.client.get("/api/activity", params={
            "kind": "reimbursement", "target": target})
        self.assertEqual(r.status_code, 200, r.text)
        return [x for x in r.json()["rows"] if x.get("action") == action]

    def test_an_unflag_that_removes_nothing_is_not_logged(self):
        """Clearing the awaiting flag twice records one unflag, not two:
        the log says what changed, like unlink."""
        charge = self._add(28.0, "LAB FEE", primary="MEDICAL")
        r = self.client.post(f"/api/reimburse/{charge}/flag", json={})
        self.assertEqual(r.status_code, 200, r.text)
        for _ in range(2):
            r = self.client.post(f"/api/reimburse/{charge}/unflag")
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self._actions(charge, "unflagged")), 1)

    def test_flagging_a_missing_transaction_is_not_found_and_not_logged(self):
        r = self.client.post("/api/reimburse/no-such-txn/flag", json={})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self._actions("no-such-txn", "flagged"), [])

    def test_an_expectation_above_the_charge_is_refused(self):
        """No link can bring back more than the charge, so a flag waiting
        for more could never clear and would alert forever."""
        charge = self._add(80.0, "DENTIST", primary="MEDICAL")
        r = self.client.post(f"/api/reimburse/{charge}/flag",
                             json={"expected": 120})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("at most the charge", r.json()["detail"])
        r = self.client.post(f"/api/reimburse/{charge}/flag",
                             json={"expected": 80})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
