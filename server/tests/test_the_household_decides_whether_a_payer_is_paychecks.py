"""The household's word on one payer, through its door.

POST /api/income/payer stores "paycheck" / "never" for one payer key, or
forgets the word with "auto"; the income window's split and its
`source_paychecks` follow at once. A key that is not a payer key, or a
word that is not one of the three, is refused. A restored settings
document keeps only {key: bool} words.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import restore

from .util import _ensure_db, add_txn, seed_accounts, write_config


class IncomePayerDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.c = TestClient(app)
        cls.c.post("/api/signup", data={
            "email": f"pay-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.c.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # a deposit in a month that has closed, whatever today is
            first = (dt.date.today().replace(day=1)
                     - dt.timedelta(days=40)).replace(day=3)
            add_txn(conn, first, -640.0, "Adatum Stipend", primary="INCOME",
                    account="chk")
        finally:
            conn.close()

    def _window(self):
        r = self.c.get("/api/reports/income/window?range=all")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _set(self, word, key="adatumstipend"):
        return self.c.post("/api/income/payer", json={
            "key": key, "count_as": word, "name": "Adatum Stipend"})

    def test_the_word_moves_the_split_and_auto_forgets_it(self):
        w = self._window()
        self.assertAlmostEqual(w["kinds"]["other"], 640.0, places=2)
        self.assertEqual(w["source_paychecks"][0],
                         {"key": "adatumstipend", "paycheck": False,
                          "override": None})
        r = self._set("paycheck")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["override"], "paycheck")
        w = self._window()
        self.assertAlmostEqual(w["kinds"]["paychecks"], 640.0, places=2)
        self.assertEqual(w["source_paychecks"][0]["override"], "paycheck")
        r = self._set("auto")
        self.assertIsNone(r.json()["override"])
        w = self._window()
        self.assertAlmostEqual(w["kinds"]["other"], 640.0, places=2)
        self.assertIsNone(w["source_paychecks"][0]["override"])

    def test_a_key_or_a_word_out_of_shape_is_refused(self):
        self.assertEqual(self._set("paycheck", key="Adatum Stipend")
                         .status_code, 400)
        self.assertEqual(self._set("paycheck", key="").status_code, 400)
        self.assertEqual(self._set("sometimes").status_code, 400)


class RestoredWords(unittest.TestCase):
    def test_a_restored_document_keeps_only_key_to_bool_words(self):
        cfg = {"paycheck_payers": {"tailspintoys": True, "Bad Key": True,
                                   "contoso": "yes", "litware": False}}
        restore.clean_config_shapes(cfg)
        self.assertEqual(cfg["paycheck_payers"],
                         {"tailspintoys": True, "litware": False})
        cfg = {"paycheck_payers": ["tailspintoys"]}
        restore.clean_config_shapes(cfg)
        self.assertNotIn("paycheck_payers", cfg)


class EveryPayerHasAKeyTheDoorTakes(unittest.TestCase):
    """The key the income window hands out is the key the door accepts, for
    any payer with a name: one written without ASCII letters or digits, or
    one whose fold runs past the key's bound, is keyed by a digest of its
    name instead. A plain name keeps the fold it always had, so a stored
    word still finds its payer."""

    def test_any_named_payer_gets_a_key_the_door_accepts(self):
        from oikonome.engine import paychecks
        names = ("\u682a\u5f0f\u4f1a\u793e\u30c6\u30b9\u30c8",
                 "\u042f\u043d\u0434\u0435\u043a\u0441", "\u2014 \u2014",
                 "Payroll " + "Northwind Deposit " * 12)
        keys = [paychecks.payer_key(n) for n in names]
        for n, k in zip(names, keys):
            self.assertTrue(paychecks.valid_key(k), (n, k))
        self.assertEqual(len(set(keys)), len(keys))
        # a payer's key is stable, and a plain name keeps its fold
        self.assertEqual(paychecks.payer_key(names[0]),
                         paychecks.payer_key(names[0]))
        self.assertEqual(paychecks.payer_key("Adatum Stipend"),
                         "adatumstipend")


if __name__ == "__main__":
    unittest.main()
