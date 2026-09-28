"""A demonstration household is a real account on a live instance: the demo
household's synthetic data, but demo_mode OFF (the native app must be able to
mint its device token), with whatever standing the installed gate gives it,
and with a view-only second login.
Unlike the demo seed it must work on an instance that already has
households."""

import unittest
import uuid

from oikonome import demo
from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db


class DemoHouseholdSeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_seeds_beside_existing_tenants_without_demo_mode(self):
        owner = f"demo-owner-{uuid.uuid4().hex[:6]}@example.dev"
        viewer = f"demo-viewer-{uuid.uuid4().hex[:6]}@example.dev"
        out = demo.seed(seed=7, years=2, email=owner, viewer=viewer)
        self.assertTrue(out["ok"])
        self.assertEqual(out["viewer_email"], viewer)
        self.assertGreaterEqual(len(out["viewer_password"]), 12)
        admin = tenancy.admin_connect()
        try:
            users = admin.execute(
                "SELECT email, role FROM users WHERE tenant_id=%s ORDER BY role",
                (out["tenant_id"],)).fetchall()
            self.assertEqual({(u["email"], u["role"]) for u in users},
                             {(owner, "owner"), (viewer, "viewer")})
        finally:
            admin.close()
        conn = tenancy.tenant_connect(out["tenant_id"])
        try:
            cfg = budget.load_config(conn)
            self.assertFalse(cfg.get("demo_mode"))
            self.assertNotIn("demo_login", cfg)
            n = conn.execute("SELECT count(*) AS n FROM transactions").fetchone()["n"]
            self.assertGreater(n, 500)
        finally:
            conn.close()
        # the same emails cannot be seeded twice
        with self.assertRaises(SystemExit):
            demo.seed(seed=7, years=2, email=owner, viewer=viewer)

    def test_plain_demo_seed_still_refuses_a_populated_instance(self):
        with self.assertRaises(SystemExit):
            demo.seed(seed=1, years=1, email=f"d-{uuid.uuid4().hex[:6]}@example.dev")


if __name__ == "__main__":
    unittest.main()
