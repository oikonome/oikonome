"""Mobile device tokens: a signed-in session mints one, the bearer then
has full session-equivalent read access, revocation (panel, password
change, reset) ends it, and the credential can neither self-propagate
(a device minting devices) nor be minted by a script token."""

import unittest
import unittest.mock
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

PASSWORD = "correct-horse-battery"


class DeviceTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.email = f"dev-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={
            "email": cls.email, "password": PASSWORD})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _mint(self, name="test phone", platform="android"):
        r = self.client.post("/api/devices", json={
            "device_name": name, "platform": platform})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _bearer(self, token):
        return TestClient(self.client.app), {
            "Authorization": f"Bearer {token}"}

    def test_mint_lists_hashless_and_marks_current(self):
        minted = self._mint("Test Phone")
        self.assertTrue(minted["token"].startswith("oikd_"))
        bare, hdr = self._bearer(minted["token"])
        listed = bare.get("/api/devices", headers=hdr).json()["devices"]
        me = [d for d in listed if d["id"] == minted["id"]]
        self.assertEqual(len(me), 1)
        self.assertNotIn("token", me[0])
        self.assertNotIn("token_hash", me[0])
        self.assertEqual(me[0]["device_name"], "Test Phone")
        self.assertTrue(me[0]["current"])
        # the web session sees the same device, not marked current
        web = self.client.get("/api/devices").json()["devices"]
        self.assertFalse([d for d in web if d["id"] == minted["id"]][0]["current"])

    def test_bearer_reads_the_app_like_a_session(self):
        minted = self._mint()
        bare, hdr = self._bearer(minted["token"])
        for path in ("/api/me", "/api/today", "/api/transactions"):
            r = bare.get(path, headers=hdr)
            self.assertEqual(r.status_code, 200, f"{path} -> {r.status_code}")
        self.assertEqual(bare.get("/api/me", headers=hdr).json()["tenant_id"],
                         self.tid)

    def test_device_cannot_mint_another_device(self):
        minted = self._mint()
        bare, hdr = self._bearer(minted["token"])
        r = bare.post("/api/devices", headers=hdr,
                      json={"device_name": "clone"})
        self.assertEqual(r.status_code, 403)

    def test_device_revokes_itself_without_stepup(self):
        minted = self._mint("logout-me")
        bare, hdr = self._bearer(minted["token"])
        r = bare.post("/api/devices/revoke", headers=hdr,
                      json={"id": minted["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(bare.get("/api/me", headers=hdr).status_code, 401)

    def test_web_revoke_of_a_device_needs_the_password(self):
        minted = self._mint("stolen?")
        r = self.client.post("/api/devices/revoke", json={"id": minted["id"]})
        self.assertEqual(r.status_code, 403)          # elevation_required
        r = self.client.post("/api/devices/revoke",
                             json={"id": minted["id"], "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        bare, hdr = self._bearer(minted["token"])
        self.assertEqual(bare.get("/api/me", headers=hdr).status_code, 401)

    def test_password_change_revokes_every_device(self):
        minted = self._mint("pre-rotation")
        r = self.client.post("/api/password/change", json={
            "current_password": PASSWORD, "new_password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        bare, hdr = self._bearer(minted["token"])
        self.assertEqual(bare.get("/api/me", headers=hdr).status_code, 401)

    def test_garbage_and_foreign_ids_are_harmless(self):
        bare = TestClient(self.client.app)
        r = bare.get("/api/me",
                     headers={"Authorization": "Bearer oikd_deadbeef"})
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/api/devices/revoke",
                             json={"id": "not-a-uuid", "password": PASSWORD})
        self.assertEqual(r.status_code, 400)
        # someone else's device id revokes nothing
        other = TestClient(self.client.app)
        other.post("/api/signup", data={
            "email": f"dev2-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PASSWORD})
        minted = self._mint("mine")
        r = other.post("/api/devices/revoke",
                       json={"id": minted["id"], "password": PASSWORD})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["revoked"], 0)
        bare, hdr = self._bearer(minted["token"])
        self.assertEqual(bare.get("/api/me", headers=hdr).status_code, 200)

    def test_script_token_cannot_touch_the_device_doors(self):
        r = self.client.post("/api/tokens",
                             json={"name": "s", "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        bare, hdr = self._bearer(r.json()["token"])
        self.assertEqual(bare.post("/api/devices", headers=hdr,
                                   json={"device_name": "x"}).status_code, 403)
        self.assertEqual(bare.get("/api/devices", headers=hdr).status_code, 403)

    def test_concurrent_mints_cannot_overshoot_the_ceiling(self):
        """Two mints racing at one-below-the-ceiling must produce exactly
        one token, not two — the count-then-insert is serialized by a
        per-user advisory lock."""
        import threading
        from oikonome.auth import device_tokens as dt_mod
        other = TestClient(self.client.app)
        email = f"race-{uuid.uuid4().hex[:8]}@example.dev"
        other.post("/api/signup", data={"email": email, "password": PASSWORD})
        tid = other.get("/api/me").json()["tenant_id"]
        conn = tenancy.control_connect()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email=%s",
                               (email,)).fetchone()["id"]
            for i in range(dt_mod.MAX_ACTIVE - 1):
                dt_mod.mint(conn, uid, tid, f"pre-{i}")
            conn.commit()
        finally:
            conn.close()
        results = []
        barrier = threading.Barrier(2)

        def one_mint():
            c = tenancy.control_connect()
            try:
                barrier.wait()
                results.append(dt_mod.mint(c, uid, tid, "racer"))
                c.commit()
            finally:
                c.close()
        threads = [threading.Thread(target=one_mint) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(1, sum(1 for r in results if r is not None),
                         "exactly one racer may claim the last slot")
        conn = tenancy.control_connect()
        try:
            n = conn.execute(
                """SELECT count(*) AS n FROM device_tokens
                   WHERE user_id=%s AND revoked_at IS NULL
                         AND expires_at > now()""", (uid,)).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, dt_mod.MAX_ACTIVE)

    def test_the_ceiling_refusal_carries_the_way_out(self):
        """A phone at the limit is sitting on a sign-in screen it cannot
        get past, so it cannot reach Settings to revoke one. The refusal
        ships the roster the picker needs."""
        from oikonome.auth import device_tokens as dt_mod
        me = self.client.get("/api/me").json()
        conn = tenancy.control_connect()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email=%s",
                               (self.email,)).fetchone()["id"]
            for i in range(dt_mod.MAX_ACTIVE):
                dt_mod.mint(conn, uid, me["tenant_id"], f"full-{i}")
            conn.commit()
            r = self.client.post("/api/devices",
                                 json={"device_name": "one too many"})
            self.assertEqual(r.status_code, 409)
            d = r.json()["detail"]
            self.assertEqual(d["error"], "device_limit")
            self.assertEqual(d["limit"], dt_mod.MAX_ACTIVE)
            self.assertEqual(len(d["devices"]), dt_mod.MAX_ACTIVE)
            one = d["devices"][0]
            for k in ("id", "device_name", "platform", "last_seen"):
                self.assertIn(k, one)
            # surrogate ids only — never anything that could act as a
            # credential, exactly like the /api/devices roster
            self.assertNotIn("token", str(d))
        finally:
            dt_mod.revoke_all_for_user(conn, uid)
            conn.commit()
            conn.close()

    def test_swapping_a_device_in_signs_the_chosen_one_out(self):
        from oikonome.auth import device_tokens as dt_mod
        me = self.client.get("/api/me").json()
        conn = tenancy.control_connect()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email=%s",
                               (self.email,)).fetchone()["id"]
            for i in range(dt_mod.MAX_ACTIVE):
                dt_mod.mint(conn, uid, me["tenant_id"], f"swap-{i}")
            conn.commit()
            victim = self.client.post(
                "/api/devices", json={"device_name": "x"}).json()
            victim_id = victim["detail"]["devices"][0]["id"]
            r = self.client.post("/api/devices",
                                 json={"device_name": "the new phone",
                                       "replace": victim_id})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["token"].startswith(dt_mod.PREFIX))
            live = [str(d["id"]) for d in dt_mod.list_for_user(conn, uid)]
            self.assertNotIn(victim_id, live, "the chosen device is out")
            self.assertEqual(len(live), dt_mod.MAX_ACTIVE,
                             "one in, one out — never over the ceiling")
        finally:
            dt_mod.revoke_all_for_user(conn, uid)
            conn.commit()
            conn.close()

    def test_a_swap_cannot_reach_another_accounts_device(self):
        from oikonome.auth import device_tokens as dt_mod
        other = TestClient(self.client.app)
        email = f"other-{uuid.uuid4().hex[:8]}@example.dev"
        other.post("/api/signup", data={"email": email, "password": PASSWORD})
        theirs = other.post("/api/devices",
                            json={"device_name": "their phone"}).json()["id"]
        r = self.client.post("/api/devices",
                             json={"device_name": "mine", "replace": theirs})
        self.assertEqual(r.status_code, 404)
        conn = tenancy.control_connect()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email=%s",
                               (email,)).fetchone()["id"]
            self.assertEqual(
                [str(d["id"]) for d in dt_mod.list_for_user(conn, uid)],
                [theirs], "their device is untouched")
        finally:
            conn.close()

    def test_a_refused_swap_never_costs_a_device(self):
        """The revoke and the mint are one transaction: if the mint still
        fails, the person must not be left one device down for nothing."""
        from oikonome.auth import device_tokens as dt_mod
        me = self.client.get("/api/me").json()
        conn = tenancy.control_connect()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email=%s",
                               (self.email,)).fetchone()["id"]
            for i in range(dt_mod.MAX_ACTIVE):
                dt_mod.mint(conn, uid, me["tenant_id"], f"atomic-{i}")
            conn.commit()
            listed = dt_mod.list_for_user(conn, uid)
            victim_id = str(listed[0]["id"])
            with unittest.mock.patch.object(dt_mod, "mint",
                                            return_value=None):
                r = self.client.post("/api/devices",
                                     json={"device_name": "n",
                                           "replace": victim_id})
            self.assertEqual(r.status_code, 409)
            live = [str(d["id"]) for d in dt_mod.list_for_user(conn, uid)]
            self.assertIn(victim_id, live,
                          "a failed swap rolled back the sign-out")
            self.assertEqual(len(live), dt_mod.MAX_ACTIVE)
        finally:
            dt_mod.revoke_all_for_user(conn, uid)
            conn.commit()
            conn.close()

    def test_active_device_ceiling_is_enforced(self):
        from oikonome.auth import device_tokens as dt_mod
        me = self.client.get("/api/me").json()
        conn = tenancy.control_connect()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email=%s",
                               (self.email,)).fetchone()["id"]
            for i in range(dt_mod.MAX_ACTIVE):
                dt_mod.mint(conn, uid, me["tenant_id"], f"bulk-{i}")
            self.assertIsNone(dt_mod.mint(conn, uid, me["tenant_id"], "over"))
            # cleanup so the other tests' user isn't left at the ceiling
            dt_mod.revoke_all_for_user(conn, uid)
        finally:
            conn.close()

    def test_a_shared_demonstration_login_evicts_its_stalest_device(self):
        """A login whose second factor is waived is shared by design: at the
        ceiling the next device must get in, and the one that goes is the
        one used longest ago — never a refusal that shows the next visitor
        a picker of other people's phones."""
        from oikonome.auth import device_tokens as dt_mod
        me = self.client.get("/api/me").json()
        conn = tenancy.control_connect()
        admin = tenancy.admin_connect()     # the app role cannot waive a factor

        def waive(flag):
            admin.execute("UPDATE users SET second_factor_waived = %s "
                          "WHERE id = %s", (flag, uid))
            admin.commit()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email=%s",
                               (self.email,)).fetchone()["id"]
            waive(True)
            for i in range(dt_mod.MAX_ACTIVE):
                dt_mod.mint(conn, uid, me["tenant_id"], f"bulk-{i}")
            minted = dt_mod.mint(conn, uid, me["tenant_id"], "visitor")
            self.assertIsNotNone(minted)
            live = [r["device_name"] for r in dt_mod.list_for_user(conn, uid)]
            self.assertEqual(len(live), dt_mod.MAX_ACTIVE)
            self.assertIn("visitor", live)
            self.assertNotIn("bulk-0", live)
        finally:
            dt_mod.revoke_all_for_user(conn, uid)
            waive(False)
            admin.close()
            conn.close()


if __name__ == "__main__":
    unittest.main()
