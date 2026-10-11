"""A phone redeeming its mint ticket while the owner rotates credentials
must not come out holding a fresh device token.

A rotation (password or email change, second-factor enrol, "sign out
everywhere else") revokes the user's device tokens and burns their unspent
mint tickets in one transaction under the per-user factor lock. A redeem
that is still in flight is invisible to that revoke, and its ticket row
lock makes the later burn find nothing — so unless the mint takes the same
lock first, a 90-day device token commits right behind a rotation that
reported every device gone.
"""

import threading
import time

from oikonome.auth import device_tokens
from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy

from .test_credential_rotation_evicts_persistence import PW, _Base


class MintWaitsForARotation(_Base):
    def _live_devices(self):
        conn = tenancy.control_connect()
        try:
            return conn.execute(
                "SELECT count(*) AS n FROM device_tokens "
                "WHERE user_id=%s AND revoked_at IS NULL",
                (self.uid,)).fetchone()["n"]
        finally:
            conn.close()

    def test_a_ticket_redeemed_mid_rotation_leaves_no_live_device(self):
        _, ticket = self._login()
        out = {}
        rot = tenancy.control_connect()
        try:
            with rot.transaction():
                # the rotation's first half: the lock, then the revoke
                reset_mod.lock_user_factors(rot, self.uid)
                device_tokens.revoke_all_for_user(rot, self.uid)
                t = threading.Thread(target=lambda: out.setdefault(
                    "r", self._redeem(ticket)))
                t.start()
                # give an unlocked mint ample time to commit its token
                time.sleep(1.5)
                # ...and its second half: burn the tickets, commit
                device_tokens.revoke_mint_tickets(rot, self.uid)
            t.join(30)
        finally:
            rot.close()
        self.assertFalse(t.is_alive())
        self.assertEqual(out["r"].status_code, 401, out["r"].text)
        self.assertEqual(self._live_devices(), 0)

    def test_a_ticket_with_no_rotation_still_mints(self):
        _, ticket = self._login()
        self.assertEqual(self._redeem(ticket).status_code, 200)
        self.assertEqual(self._live_devices(), 1)

    def test_sign_out_everywhere_else_waits_on_the_factor_lock(self):
        """'Sign out everywhere else' is a rotation door like the others:
        it must serialise on the factor lock, or a mint holding it can
        still slip its token past the revoke."""
        out = {}
        hold = tenancy.control_connect()
        try:
            with hold.transaction():
                reset_mod.lock_user_factors(hold, self.uid)
                t = threading.Thread(target=lambda: out.setdefault(
                    "r", self.client.post(
                        "/api/sessions/revoke",
                        json={"all_others": True, "password": PW})))
                t.start()
                time.sleep(1.5)
                self.assertTrue(t.is_alive(),
                                "the revoke ran without the factor lock")
            t.join(30)
        finally:
            hold.close()
        self.assertEqual(out["r"].status_code, 200, out["r"].text)
