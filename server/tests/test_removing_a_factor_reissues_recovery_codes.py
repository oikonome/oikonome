"""Removing a second factor while another one stays voids the old
recovery-code sheet and hands back a new one.

A sheet a hijacker minted or photographed is a live bypass for whatever
factor is left: it signs in past the remaining authenticator or passkey
and opens an elevation window. Turning TOTP off while a passkey remains,
and deleting a passkey while TOTP or another passkey remains, are the
moments the owner believes that is over, so the batch is replaced there —
the same way replacing the authenticator replaces it. The reply carries
the new batch so the owner is never left without codes they have seen.

Only a client that says it shows the new sheet (reissue_codes) gets one.
An app build that predates the field would discard the batch, so its
caller keeps the sheet it already saved rather than losing it unseen.

Where no factor remains (an operator-waived account dropping its last
one) nothing is minted: there is no factor left for codes to stand in
for.
"""

import os
import threading
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import recovery
from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy

from .test_credential_rotation_evicts_persistence import (
    PW, _Base, _fresh_code)


class _Factors(_Base):
    def _login_with(self, code):
        anon = TestClient(self.app)
        return anon.post("/api/login", data={
            "email": self.email, "password": PW, "totp_code": code})

    def _signed_in_with(self, code):
        c = TestClient(self.app)
        r = c.post("/api/login", data={
            "email": self.email, "password": PW, "totp_code": code})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.get("/api/me").status_code, 200)
        return c

    def _passkey_ids(self):
        r = self.client.get("/api/passkeys")
        self.assertEqual(r.status_code, 200, r.text)
        return [p["id"] for p in r.json()["passkeys"]]

    def _remaining(self):
        conn = tenancy.control_connect()
        try:
            return recovery.remaining(conn, self.uid)
        finally:
            conn.close()

    def _totp_then_passkey(self):
        """TOTP first (its batch is the sheet), then a passkey beside it,
        which keeps that batch."""
        secret = self.client.post("/api/totp/enroll",
                                  data={"password": PW}).json()["secret"]
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        old = r.json()["recovery_codes"]
        self.assertTrue(old)
        added = self._enroll_passkey(totp_code=_fresh_code(secret))
        self.assertIsNone(added.get("recovery_codes"))
        return secret, old

    def _assert_fresh_batch(self, body, old):
        fresh = body.get("recovery_codes")
        self.assertTrue(fresh, "the reply carries no new recovery codes")
        self.assertEqual(len(fresh), recovery.BATCH)
        self.assertFalse(set(fresh) & set(old))
        self.assertEqual(self._remaining(), recovery.BATCH)
        r = self._login_with(old[-1])
        self.assertEqual(r.status_code, 401, r.text)
        r = self._login_with(fresh[0])
        self.assertEqual(r.status_code, 200, r.text)


class TurningTotpOffReissues(_Factors):
    def test_turning_totp_off_beside_a_passkey_voids_the_old_sheet(self):
        secret, old = self._totp_then_passkey()
        r = self.client.post("/api/totp/disable", data={
            "code": _fresh_code(secret), "password": PW,
            "reissue_codes": "1"})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_fresh_batch(r.json(), old)

    def test_turning_off_the_last_factor_mints_nothing(self):
        """A waived account may drop its only factor; no codes are minted
        for a factor that no longer exists, and the existing rows are left
        as they were."""
        secret = self._enroll_totp()
        before = self._remaining()
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET second_factor_waived=TRUE "
                          "WHERE id=%s", (self.uid,))
        finally:
            admin.close()
        os.environ["OIKONOME_HOSTED"] = "1"
        r = self.client.post("/api/totp/disable", data={
            "code": _fresh_code(secret), "password": PW,
            "reissue_codes": "1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json().get("recovery_codes"))
        self.assertEqual(self._remaining(), before)


class DeletingAPasskeyReissues(_Factors):
    def test_deleting_a_passkey_beside_totp_voids_the_old_sheet(self):
        secret, old = self._totp_then_passkey()
        (pk,) = self._passkey_ids()
        r = self.client.request("DELETE", f"/api/passkeys/{pk}", json={
            "password": PW, "totp_code": _fresh_code(secret),
            "reissue_codes": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["signed_out"])
        self._assert_fresh_batch(r.json(), old)

    def test_deleting_one_of_two_passkeys_voids_the_old_sheet(self):
        old = self._enroll_passkey()["recovery_codes"]
        self._enroll_passkey(recovery_code=old[0])
        first = self._passkey_ids()[0]
        r = self.client.request("DELETE", f"/api/passkeys/{first}", json={
            "password": PW, "recovery_code": old[1], "reissue_codes": True})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_fresh_batch(r.json(), old)
        self.assertEqual(len(self._passkey_ids()), 1)

    def test_deleting_the_last_passkey_mints_nothing(self):
        old = self._enroll_passkey()["recovery_codes"]
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET second_factor_waived=TRUE "
                          "WHERE id=%s", (self.uid,))
        finally:
            admin.close()
        (pk,) = self._passkey_ids()
        r = self.client.request("DELETE", f"/api/passkeys/{pk}", json={
            "password": PW, "recovery_code": old[0], "reissue_codes": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json().get("recovery_codes"))
        self.assertEqual(self._remaining(), recovery.BATCH - 1)

    def test_deleting_a_passkey_that_is_not_there_mints_nothing(self):
        secret, old = self._totp_then_passkey()
        r = self.client.request(
            "DELETE", f"/api/passkeys/{uuid.uuid4()}", json={
                "password": PW, "totp_code": _fresh_code(secret),
                "reissue_codes": True})
        self.assertEqual(r.status_code, 404, r.text)
        conn = tenancy.control_connect()
        try:
            self.assertTrue(recovery.redeem(conn, self.uid, old[0]))
        finally:
            conn.close()


class OlderClientsKeepTheirSheet(_Factors):
    """A caller that does not send reissue_codes cannot show a new batch:
    voiding its sheet would leave codes that fail and none it has seen."""

    def _still_redeems(self, code):
        conn = tenancy.control_connect()
        try:
            self.assertTrue(recovery.redeem(conn, self.uid, code))
        finally:
            conn.close()

    def test_turning_totp_off_without_the_field_keeps_the_sheet(self):
        secret, old = self._totp_then_passkey()
        r = self.client.post("/api/totp/disable", data={
            "code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json().get("recovery_codes"))
        self._still_redeems(old[-1])

    def test_deleting_a_passkey_without_the_field_keeps_the_sheet(self):
        secret, old = self._totp_then_passkey()
        (pk,) = self._passkey_ids()
        r = self.client.request("DELETE", f"/api/passkeys/{pk}", json={
            "password": PW, "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json().get("recovery_codes"))
        self._still_redeems(old[-1])


class ReissueOnDeleteClosesTheHijack(_Factors):
    def test_a_session_evicted_while_waiting_gets_no_batch(self):
        """The delete waits for the factor lock after its step-up; a
        password reset or authenticator swap that evicted this session in
        between must leave it with a 401, the key still there and the
        owner's sheet intact."""
        secret, old = self._totp_then_passkey()
        (pk,) = self._passkey_ids()
        real = self.appmod._lock_user_factors

        def evict_then_lock(conn, user_id):
            admin = tenancy.admin_connect()
            try:
                admin.execute("DELETE FROM sessions WHERE user_id=%s",
                              (user_id,))
            finally:
                admin.close()
            return real(conn, user_id)

        self.appmod._lock_user_factors = evict_then_lock
        try:
            r = self.client.request("DELETE", f"/api/passkeys/{pk}", json={
                "password": PW, "totp_code": _fresh_code(secret),
                "reissue_codes": True})
        finally:
            self.appmod._lock_user_factors = real
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._passkey_ids_admin(), 1)
        self.assertEqual(self._remaining(), recovery.BATCH)
        conn = tenancy.control_connect()
        try:
            self.assertTrue(recovery.redeem(conn, self.uid, old[0]))
        finally:
            conn.close()

    def _passkey_ids_admin(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT count(*) AS n FROM passkeys WHERE user_id=%s",
                (self.uid,)).fetchone()["n"]
        finally:
            admin.close()

    def test_other_sessions_and_devices_are_signed_out_with_the_old_sheet(self):
        """A second session (a hijacker who signed in with a code from the
        old sheet) or a planted device token must not survive the reissue:
        signed in, it could elevate and call recovery-regenerate for the
        new batch. The caller stays signed in."""
        secret, old = self._totp_then_passkey()
        other = self._signed_in_with(old[0])
        phone = self._signed_in_with(old[1])
        m = phone.post("/api/devices", json={"device_name": "phone",
                                             "platform": "android"})
        self.assertEqual(m.status_code, 200, m.text)
        bearer = TestClient(self.app)
        bearer.cookies.clear()
        bearer.headers["Authorization"] = "Bearer " + m.json()["token"]
        self.assertEqual(bearer.get("/api/me").status_code, 200)
        (pk,) = self._passkey_ids()
        r = self.client.request("DELETE", f"/api/passkeys/{pk}", json={
            "password": PW, "totp_code": _fresh_code(secret),
            "reissue_codes": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["recovery_codes"])
        self.assertEqual(r.json()["other_sessions_signed_out"], 2)
        self.assertEqual(other.get("/api/me").status_code, 401)
        self.assertEqual(bearer.get("/api/me").status_code, 401)
        self.assertEqual(self.client.get("/api/me").status_code, 200)

    def test_without_the_field_nobody_is_signed_out(self):
        secret, old = self._totp_then_passkey()
        other = self._signed_in_with(old[0])
        (pk,) = self._passkey_ids()
        r = self.client.request("DELETE", f"/api/passkeys/{pk}", json={
            "password": PW, "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["other_sessions_signed_out"], 0)
        self.assertEqual(other.get("/api/me").status_code, 200)


class TheOptInIsReadLeniently(_Factors):
    def test_a_string_true_still_reissues(self):
        secret, old = self._totp_then_passkey()
        (pk,) = self._passkey_ids()
        r = self.client.request("DELETE", f"/api/passkeys/{pk}", json={
            "password": PW, "totp_code": _fresh_code(secret),
            "reissue_codes": "true"})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_fresh_batch(r.json(), old)

    def test_a_form_true_still_reissues(self):
        secret, old = self._totp_then_passkey()
        r = self.client.post("/api/totp/disable", data={
            "code": _fresh_code(secret), "password": PW,
            "reissue_codes": "true"})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_fresh_batch(r.json(), old)



class ASignInOnAVoidedSheetGetsNoSession(_Factors):
    """A sign-in spends its recovery code before it takes the factor
    lock. A reissue that lands in that gap has already swept every
    session it could see, so the sign-in must find, under the lock, that
    the sheet its code came from is gone — or it opens a session the
    sweep never saw."""

    def _sessions(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT count(*) AS n FROM sessions WHERE user_id=%s",
                (self.uid,)).fetchone()["n"]
        finally:
            admin.close()

    def test_a_reissue_between_the_redeem_and_the_session_refuses_it(self):
        secret, old = self._totp_then_passkey()
        real = self.appmod._login_response
        uid = self.uid

        def reissue_then_respond(*a, **kw):
            admin = tenancy.admin_connect()
            try:
                recovery.issue(admin, uid)
            finally:
                admin.close()
            return real(*a, **kw)

        before = self._sessions()
        self.appmod._login_response = reissue_then_respond
        try:
            r = self._login_with(old[0])
        finally:
            self.appmod._login_response = real
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._sessions(), before)

    def test_a_code_from_the_standing_sheet_still_signs_in(self):
        secret, old = self._totp_then_passkey()
        r = self._login_with(old[0])
        self.assertEqual(r.status_code, 200, r.text)


class TheSheetChangesOnlyWithTheChange(_Factors):
    """The new batch and the change that voids the old one commit
    together. If anything after the mint fails, the change rolls back —
    and so must the void, or the owner is left with an old sheet that no
    longer works and a new one they were never shown."""

    def _failing_after_issue(self):
        real = recovery.issue

        def issue_then_fail(admin, user_id, *a, **kw):
            real(admin, user_id, *a, **kw)
            raise RuntimeError("the transaction around the mint failed")
        return issue_then_fail

    def _still_redeems(self, code):
        conn = tenancy.control_connect()
        try:
            self.assertTrue(recovery.redeem(conn, self.uid, code))
        finally:
            conn.close()

    def _quiet(self):
        return TestClient(self.app, raise_server_exceptions=False,
                          cookies=self.client.cookies)

    def test_a_failed_passkey_removal_keeps_the_old_sheet(self):
        secret, old = self._totp_then_passkey()
        (pk,) = self._passkey_ids()
        recovery.issue, real = self._failing_after_issue(), recovery.issue
        try:
            r = self._quiet().request("DELETE", f"/api/passkeys/{pk}", json={
                "password": PW, "totp_code": _fresh_code(secret),
                "reissue_codes": True})
        finally:
            recovery.issue = real
        self.assertEqual(r.status_code, 500, r.text)
        self.assertEqual(len(self._passkey_ids()), 1)
        self._still_redeems(old[-1])

    def test_a_failed_regenerate_keeps_the_old_sheet(self):
        secret, old = self._totp_then_passkey()
        recovery.issue, real = self._failing_after_issue(), recovery.issue
        try:
            r = self._quiet().post("/api/totp/recovery-regenerate", data={
                "password": PW, "totp_code": _fresh_code(secret)})
        finally:
            recovery.issue = real
        self.assertEqual(r.status_code, 500, r.text)
        self._still_redeems(old[-1])

    def test_a_failed_totp_disable_keeps_the_old_sheet(self):
        secret, old = self._totp_then_passkey()
        recovery.issue, real = self._failing_after_issue(), recovery.issue
        try:
            r = self._quiet().post("/api/totp/disable", data={
                "code": _fresh_code(secret), "password": PW,
                "reissue_codes": "1"})
        finally:
            recovery.issue = real
        self.assertEqual(r.status_code, 500, r.text)
        self._still_redeems(old[-1])


class TheNoticeSaysTheOldSheetIsVoid(_Factors):
    """The owner's printed sheet dies with a reissue. The mail is the only
    place a hijacked session's removal can be noticed, so it says so —
    and it does not claim sign-outs a door never made."""

    def _notices(self, fn):
        seen = []
        real = self.appmod._notify_security_change
        self.appmod._notify_security_change = (
            lambda user, change: seen.append(change))
        try:
            fn()
        finally:
            self.appmod._notify_security_change = real
        return seen

    def _body(self, change):
        from unittest import mock
        sent = []
        with mock.patch.object(self.appmod, "_smtp_for",
                               return_value={"configured": True}), \
                mock.patch("oikonome.web.report.send",
                           lambda subj, plain, html, to, **kw:
                           sent.append(plain)), \
                mock.patch.object(self.appmod, "_security_fan_out"):
            self.appmod._deliver_security_notice(
                "owner@example.dev", None, change)
        self.assertEqual(len(sent), 1, change)
        return sent[0]

    def test_a_reissuing_passkey_removal_says_the_codes_changed(self):
        secret, old = self._totp_then_passkey()
        (pk,) = self._passkey_ids()

        def delete():
            r = self.client.request("DELETE", f"/api/passkeys/{pk}", json={
                "password": PW, "totp_code": _fresh_code(secret),
                "reissue_codes": True})
            self.assertEqual(r.status_code, 200, r.text)
        (change,) = self._notices(delete)
        body = self._body(change)
        self.assertIn("passkey was removed", body)
        self.assertIn("old ones no longer work", body)
        self.assertIn("signed out", body)

    def test_a_reissuing_totp_disable_says_the_codes_changed(self):
        secret, old = self._totp_then_passkey()

        def disable():
            r = self.client.post("/api/totp/disable", data={
                "code": _fresh_code(secret), "password": PW,
                "reissue_codes": "1"})
            self.assertEqual(r.status_code, 200, r.text)
        (change,) = self._notices(disable)
        self.assertIn("old ones no longer work", self._body(change))

    def test_a_removal_that_signs_nobody_out_does_not_say_it_did(self):
        secret, old = self._totp_then_passkey()
        (pk,) = self._passkey_ids()

        def delete():
            r = self.client.request("DELETE", f"/api/passkeys/{pk}", json={
                "password": PW, "totp_code": _fresh_code(secret)})
            self.assertEqual(r.status_code, 200, r.text)
        (change,) = self._notices(delete)
        body = self._body(change)
        self.assertIn("passkey was removed", body)
        self.assertNotIn("signed out", body)
        self.assertNotIn("no longer work", body)


class OneSheetStandsWhenReissuesOverlap(_Factors):
    """A staged batch commits after the door's own transaction, so after the
    factor lock is released. A second reissue (or a reset's drop) that runs
    in that gap must still see the first batch and void it: two overlapping
    reissues leave exactly one sheet, the later one, and a drop leaves none.
    """

    def _door_a_paused(self):
        """Door A up to the gap: its control transaction (with the factor
        lock) has committed, its staged batch has not. Returns the open
        staged batch and the codes it minted."""
        staged = recovery.issue_staged(tenancy.admin_connect)
        staged.__enter__()
        conn = tenancy.control_connect()
        try:
            with conn.transaction():
                reset_mod.lock_user_factors(conn, self.uid)
                codes = staged.issue(self.uid)
        finally:
            conn.close()
        return staged, codes

    def _door_b(self, out):
        try:
            with recovery.issue_staged(tenancy.admin_connect) as b:
                conn = tenancy.control_connect()
                try:
                    with conn.transaction():
                        reset_mod.lock_user_factors(conn, self.uid)
                        out["codes"] = b.issue(self.uid)
                finally:
                    conn.close()
        except BaseException as e:      # surfaced by the test thread
            out["error"] = e

    def _run_beside_paused_a(self, target):
        """Start `target` in a thread while A sits in the gap, give it time
        to reach the code table (it waits there, or runs straight through),
        then let A commit."""
        staged, codes_a = self._door_a_paused()
        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(1.0)                      # B reaches its sweep, or finishes
        staged.__exit__(None, None, None)
        t.join(15)
        self.assertFalse(t.is_alive(), "the second writer never finished")
        return codes_a

    def _a_writer_waits(self):
        admin = tenancy.admin_connect()
        try:
            return bool(admin.execute(
                "SELECT 1 FROM pg_locks WHERE NOT granted").fetchone())
        finally:
            admin.close()

    def _redeems(self, code):
        admin = tenancy.admin_connect()
        try:
            with admin.transaction():
                ok = recovery.redeem(admin, self.uid, code)
                raise _Undo()
        except _Undo:
            return ok
        finally:
            admin.close()

    def test_two_overlapping_reissues_leave_only_the_later_sheet(self):
        self._enroll_totp()              # S0 on file
        out = {}
        codes_a = self._run_beside_paused_a(lambda: self._door_b(out))
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual(self._remaining(), recovery.BATCH)
        self.assertFalse(self._redeems(codes_a[0]),
                         "the first reissue's sheet still redeems")
        self.assertTrue(self._redeems(out["codes"][0]))

    def test_a_drop_during_a_reissue_leaves_no_sheet(self):
        self._enroll_totp()
        out = {}

        def drop():
            try:
                reset_mod.drop_recovery_codes(self.uid)
            except BaseException as e:
                out["error"] = e
        codes_a = self._run_beside_paused_a(drop)
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual(self._remaining(), 0)
        self.assertFalse(self._redeems(codes_a[0]))

    def test_a_member_moved_out_during_a_reissue_keeps_no_sheet(self):
        """The signup reclaim that moves an unverified member out of a
        household burns that row's codes. A reissue still committing its
        batch when the move runs must be waited for and dropped with the
        rest, or the sheet minted in the old household follows the row
        into the new one."""
        from oikonome.auth import signup_reclaim
        victim = f"mov-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            with admin.transaction():
                tid = admin.execute("SELECT tenant_id FROM users WHERE id=%s",
                                    (self.uid,)).fetchone()["tenant_id"]
                vid = admin.execute(
                    "INSERT INTO users (tenant_id, email, password_hash, "
                    "role) VALUES (%s, %s, 'x', 'member') RETURNING id",
                    (tid, victim)).fetchone()["id"]
                token = signup_reclaim.create(admin, vid, victim, None, None)
        finally:
            admin.close()
        self.uid = vid                   # the paused reissue is the member's
        out = {}

        def move_out():
            try:
                out["r"] = TestClient(self.app).post(
                    "/signup/reclaim",
                    data={"token": token, "password": "a-new-own-password"},
                    follow_redirects=False)
            except BaseException as e:
                out["error"] = e
        # the move hashes a password first, so a fixed pause can let A
        # commit before the move reaches the code table: wait instead until
        # the move has either finished or is waiting on a lock
        staged, codes_a = self._door_a_paused()
        t = threading.Thread(target=move_out, daemon=True)
        t.start()
        for _ in range(300):
            t.join(0.05)
            if not t.is_alive() or self._a_writer_waits():
                break
        staged.__exit__(None, None, None)
        t.join(15)
        self.assertFalse(t.is_alive(), "the move never finished")
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual(out["r"].status_code, 303, out["r"].text)
        conn = tenancy.control_connect()
        try:
            moved = conn.execute("SELECT tenant_id FROM users WHERE id=%s",
                                 (vid,)).fetchone()["tenant_id"]
        finally:
            conn.close()
        self.assertNotEqual(moved, tid)
        self.assertEqual(self._remaining(), 0,
                         "the reissue's sheet followed the member out")
        self.assertFalse(self._redeems(codes_a[0]))


class _Undo(Exception):
    pass


if __name__ == "__main__":
    import unittest
    unittest.main()
