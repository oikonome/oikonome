"""Top holdings ranks only the positions the net-worth figures count.

A brokerage linked through two sources is one account: the non-primary is
a shadow, and listing its positions would show every fund twice and push
real positions out of the capped list. A hidden account contributes to
nothing, so its positions do not rank either. A business entity's account
stays out of the personal view unless the household combines entities.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import links

from .util import _ensure_db, seed_accounts


class TopHoldingsCountEachPositionOnce(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"holdings-{uuid.uuid4().hex[:10]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            for aid, name in (("brk-a", "Brokerage via A"),
                              ("brk-b", "Brokerage via B"),
                              ("brk-hidden", "Old Brokerage")):
                conn.execute(
                    "INSERT INTO accounts (id,item_id,name,type,subtype,"
                    "balance_current) VALUES (%s,'it1',%s,'investment',"
                    "'brokerage',120000)", (aid, name))
                conn.execute(
                    "INSERT INTO holdings (account_id,symbol,name,quantity,"
                    "price,value) VALUES (%s,'VTSAX','Total Stock',1000,"
                    "120,120000)", (aid,))
            from oikonome.engine import entities
            biz = entities.create_entity(conn, name="Side Studio LLC",
                                         structure="sole_prop")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current,entity_id) VALUES ('brk-biz','it1',"
                "'Studio Brokerage','investment','brokerage',90000,%s)",
                (biz["id"],))
            conn.execute(
                "INSERT INTO holdings (account_id,symbol,name,quantity,"
                "price,value) VALUES ('brk-biz','VTIAX','Intl Stock',1000,"
                "90,90000)")
            links.create(conn, ["brk-a", "brk-b"])
            conn.execute("UPDATE accounts SET user_removed_at=now() "
                         "WHERE id='brk-hidden'")
        finally:
            conn.close()

    def test_a_linked_or_hidden_brokerage_does_not_repeat_a_position(self):
        r = self.client.get("/api/holdings")
        self.assertEqual(r.status_code, 200, r.text)
        accts = [h["acct"] for h in r.json()["holdings"]
                 if h["symbol"] == "VTSAX"]
        self.assertEqual(accts, ["Brokerage via A"])

    def test_a_business_entitys_position_stays_out_of_the_personal_list(self):
        r = self.client.get("/api/holdings")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("Studio Brokerage",
                         {h["acct"] for h in r.json()["holdings"]})


if __name__ == "__main__":
    unittest.main()
