"""The settings document is read whole on nearly every request, so no door
may let a caller grow it without limit: the one write path refuses a save
that would take it past its bound (answered 413, never 500), and the doors
that append payee- or suggestion-keyed entries take only bounded keys the
server could have offered."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class SettingsDocumentBoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.c = TestClient(app)
        cls.c.post("/api/signup", data={
            "email": f"cfgbound-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.c.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _cfg(self):
        from oikonome.engine import budget
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def test_a_write_growing_the_document_past_its_bound_is_413(self):
        from oikonome.engine import budget
        r = self.c.post("/api/bills/save", json={
            "payee": "Bound Co", "amount": 20, "cadence": "MONTHLY:1",
            "next_due": "2026-09-22"})
        self.assertEqual(r.status_code, 200, r.text)
        size = budget._config_bytes(self._cfg())
        with mock.patch.object(budget, "CONFIG_MAX_BYTES", size + 5):
            r = self.c.post("/api/bills/toggle",
                            json={"payee": "Bound Co", "disabled": True})
        self.assertEqual(r.status_code, 413, r.text)
        self.assertNotIn("Bound Co", self._cfg().get("disabled_bills") or [])

    def test_a_document_already_over_the_bound_can_still_shrink(self):
        from oikonome.engine import budget
        r = self.c.post("/api/bills/save", json={
            "payee": "Shrink Co", "amount": 20, "cadence": "MONTHLY:1",
            "next_due": "2026-09-22"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.c.post("/api/bills/toggle",
                        json={"payee": "Shrink Co", "disabled": True})
        self.assertEqual(r.status_code, 200, r.text)
        with mock.patch.object(budget, "CONFIG_MAX_BYTES", 10):
            r = self.c.post("/api/bills/toggle",
                            json={"payee": "Shrink Co", "disabled": False})
        self.assertEqual(r.status_code, 200, r.text)

    def test_pausing_a_payee_no_bill_carries_is_refused(self):
        r = self.c.post("/api/bills/toggle",
                        json={"payee": "x" * 5000, "disabled": True})
        self.assertEqual(r.status_code, 400, r.text)
        r = self.c.post("/api/bills/toggle",
                        json={"payee": "Nobody Co", "disabled": True})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertNotIn("Nobody Co", self._cfg().get("disabled_bills") or [])

    def test_payer_words_are_capped(self):
        from oikonome.engine import budget, paychecks
        from oikonome.web import api_bills
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg[paychecks.CONFIG_KEY] = {
                    f"payer{i}": True for i in range(api_bills.PAYER_WORDS_MAX)}
        finally:
            conn.close()
        try:
            r = self.c.post("/api/income/payer",
                            json={"key": "onemorepayer", "count_as": "never"})
            self.assertEqual(r.status_code, 409, r.text)
            # an existing word can still change, and auto still clears
            r = self.c.post("/api/income/payer",
                            json={"key": "payer1", "count_as": "never"})
            self.assertEqual(r.status_code, 200, r.text)
            r = self.c.post("/api/income/payer",
                            json={"key": "payer2", "count_as": "auto"})
            self.assertEqual(r.status_code, 200, r.text)
        finally:
            conn = tenancy.tenant_connect(self.tid)
            try:
                with budget.config_txn(conn) as cfg:
                    cfg.pop(paychecks.CONFIG_KEY, None)
            finally:
                conn.close()

    def test_link_dismissal_keeps_only_offerable_pairs(self):
        r = self.c.post("/api/accounts/links/dismiss",
                        json={"key": "made|up"})
        self.assertEqual(r.status_code, 404, r.text)
        r = self.c.post("/api/accounts/links/dismiss",
                        json={"key": "chk|" + "y" * 5000})
        self.assertEqual(r.status_code, 400, r.text)
        r = self.c.post("/api/accounts/links/dismiss",
                        json={"key": "card|chk"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("card|chk", self._cfg().get("link_dismissed") or [])


if __name__ == "__main__":
    unittest.main()
