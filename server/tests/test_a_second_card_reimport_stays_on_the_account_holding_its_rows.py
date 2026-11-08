"""A household that once filed two cards sharing their last four on one
hand-made account keeps that account for both after the upgrade.

A build before opaque ids gave both cards the one slug id their cut name
leads to, and hashed each card's rows under it. Now the first card's
re-import claims that account, and the second card — a different full
number — is not handed an account answering to another card. That is
right for a card the household never filed there, and wrong for one it
did: a fresh account hashes the card's rows under a fresh id, and the
re-import inserts the card's whole history a second time beside the rows
already held. So a new-account name whose file's rows the earlier
account already holds stays on it, through every door that takes a typed
name: the folder import and the single-file import.

Every name, number and amount here is invented."""
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import account_sources
from oikonome.web import bulk_import, pages

from .bulk_job_util import csv_for, submit_and_run
from .util import _ensure_db, make_db, write_config

CARD_A = "Visa 4000111122220123"
CARD_B = "Visa 4000333344440123"          # same last four, another card
OLD = "manual:visa-0123"


def _seed_old_account(conn) -> None:
    """The account a build before opaque ids made for both cards, holding
    each card's statement rows under its slug id."""
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name)"
        " VALUES ('manual','manual','Manual')"
        " ON CONFLICT (tenant_id, id) DO NOTHING")
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, subtype)"
        " VALUES (%s,'manual','Visa …0123','credit','credit card')", (OLD,))
    account_sources.backfill(conn)
    for card, tag in ((CARD_A, "HARBOR"), (CARD_B, "NORTHWIND")):
        with conn.transaction():
            result, mapping = pages._dispatch_import_unlocked(
                conn, OLD, "bank", f"{card}.csv", csv_for(tag))
        assert mapping is None and not result.get("error"), result


def _ledger(conn) -> tuple[int, float]:
    r = conn.execute("SELECT count(*) AS n, COALESCE(sum(amount), 0) AS s"
                     "  FROM transactions WHERE account_id LIKE 'manual:%'"
                     ).fetchone()
    return r["n"], float(r["s"])


class TheFolderImportKeepsTheSecondCard(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        _seed_old_account(self.conn)

    def _run(self, card, tag):
        plan = bulk_import.analyze(self.conn, [(f"{card}.csv", csv_for(tag))])
        entry = dict(plan["files"][0], account_id=None,
                     new_account_name=card, amount_sign="bank")
        return submit_and_run(self.conn, plan["token"], [entry])[0]

    def test_each_card_reimported_stays_where_its_rows_are(self):
        before = _ledger(self.conn)
        for card, tag in ((CARD_A, "HARBOR"), (CARD_B, "NORTHWIND")):
            res = self._run(card, tag)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["account_id"], OLD, (card, res))
            self.assertEqual(_ledger(self.conn), before, card)
        # remembered: the next statement of the second card goes straight
        # there, and a card that was never filed there still gets its own
        with self.conn.transaction():
            self.assertEqual(bulk_import._ensure_account(self.conn, CARD_B),
                             OLD)
        res = self._run("Visa 4000555566660123", "CONTOSO")
        self.assertTrue(res["ok"], res)
        self.assertNotEqual(res["account_id"], OLD)

    def test_a_new_card_sharing_the_last_four_gets_its_own_account(self):
        self.assertEqual(self._run(CARD_A, "HARBOR")["account_id"], OLD)
        before = _ledger(self.conn)
        res = self._run("Visa 4000555566660123", "CONTOSO")
        self.assertTrue(res["ok"], res)
        self.assertNotEqual(res["account_id"], OLD)
        n, s = _ledger(self.conn)
        self.assertEqual((n, round(s, 2)), (before[0] + 3,
                                            round(before[1] + 60.0, 2)))


class TheSingleFileImportKeepsTheSecondCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        from oikonome.web import security
        appmod.DEV_MODE = True
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"card-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            write_config(conn)
            _seed_old_account(conn)
        finally:
            conn.close()

    def _ledger(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return _ledger(conn)
        finally:
            conn.close()

    def test_each_card_reimported_stays_where_its_rows_are(self):
        before = self._ledger()
        for card, tag in ((CARD_A, "HARBOR"), (CARD_B, "NORTHWIND")):
            r = self.client.post(
                "/api/import",
                data={"account_id": "", "amount_sign": "bank",
                      "new_account_name": card},
                files={"file": (f"{card}.csv", csv_for(tag), "text/csv")})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse((r.json()["result"] or {}).get("error"), r.text)
            self.assertEqual(self._ledger(), before, card)
        conn = tenancy.tenant_connect(self.tid)
        try:
            n = conn.execute("SELECT count(*) AS n FROM accounts"
                             " WHERE item_id = 'manual'").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
