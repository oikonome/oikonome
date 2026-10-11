"""The settings save refuses a savings goal that has match words but no
account, because such a goal can never be measured (the words match both
legs of a transfer and nothing says which side is the fund).

Both goal editors and the balance planner post the WHOLE list of goals on
every save, so a goal stored before this rule existed rides along on saves
that never touched it. Refusing that untouched row would lock every later
save of the list, a sibling's edit and the planner's own row included. So a
stored token-only goal posted back unchanged is still accepted; changing it
in any way that keeps the unmeasurable shape is refused."""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db, seed_accounts, write_config

LEGACY = {"name": "Trip", "target": 1000.0, "monthly_plan": 250.0,
          "account_id": None, "tokens": ["trip"], "start_balance": 0.0}
SIBLING = {"name": "Rainy day", "target": 3000.0, "monthly_plan": 100.0,
           "account_id": "chk", "tokens": [], "start_balance": 0.0}


def _make_client() -> TestClient:
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


class GoalMatchWordsNeedAnAccount(unittest.TestCase):

    def setUp(self):
        self.client = _make_client()
        self.client.post("/api/signup", data={
            "email": f"goalacct-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _store(self, *goals):
        """Write goals straight into the stored config, the way a save from
        before the rule, a restore or the mirror left them."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            cfg["savings_goals"] = [dict(g) for g in goals]
            budget.save_config(conn, cfg)
        finally:
            conn.close()

    def _save(self, *goals):
        return self.client.post("/api/settings",
                                json={"savings_goals": [dict(g)
                                                        for g in goals]})

    def _goals(self):
        return self.client.get("/api/settings").json()["savings_goals"]

    def test_a_new_goal_with_words_and_no_account_is_refused(self):
        self._save(SIBLING)
        before = self._goals()
        r = self._save(SIBLING, LEGACY)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("need an account", r.json()["detail"])
        self.assertEqual(self._goals(), before)

    def test_the_same_goal_with_an_account_is_saved(self):
        r = self._save(dict(LEGACY, account_id="chk"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._goals()[0]["account_id"], "chk")

    def test_a_stored_legacy_goal_posted_back_unchanged_lets_a_sibling_save(self):
        self._store(LEGACY, SIBLING)
        r = self._save(LEGACY, dict(SIBLING, monthly_plan=150.0))
        self.assertEqual(r.status_code, 200, r.text)
        got = {g["name"]: g for g in self._goals()}
        self.assertEqual(got["Rainy day"]["monthly_plan"], 150.0)
        self.assertEqual(got["Trip"]["tokens"], ["trip"])
        self.assertIsNone(got["Trip"]["account_id"])

    def test_adding_a_word_to_a_stored_legacy_goal_is_refused(self):
        self._store(LEGACY, SIBLING)
        r = self._save(dict(LEGACY, tokens=["trip", "travel"]), SIBLING)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("need an account", r.json()["detail"])

    def test_renaming_a_stored_legacy_goal_is_refused(self):
        self._store(LEGACY)
        r = self._save(dict(LEGACY, name="Vacation"))
        self.assertEqual(r.status_code, 400, r.text)

    def test_clearing_the_account_of_a_worded_goal_is_refused(self):
        self._store(dict(LEGACY, account_id="chk"))
        r = self._save(LEGACY)
        self.assertEqual(r.status_code, 400, r.text)

    def test_the_planners_wordless_savings_row_needs_no_account(self):
        r = self._save({"name": "Savings", "target": 0, "monthly_plan": 0})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
