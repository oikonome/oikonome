"""A bank-link hold whose keys were cleared leaves nothing behind that a
later write could inherit.

Clearing the household's own Plaid or MX keys lifts the hold on that
provider. The raw record must go with it (or stop naming that client):

- a record left in place is what a later rotation that removed a member
  merges into, so the banner would show the old record's date and its
  reason ("your account was recovered") for a password change;
- the client it named would be held again the moment the owner saved
  that same client id, though keys saved after the rotation are the
  owner's own.

A record that still pauses something else stays, naming only what is
still held; clearing one provider leaves the other provider's hold."""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.notify import outbound_pause as op

from .bank_link_hold_fixtures import (MX_CLIENT, PLAID_CLIENT, arm_hold,
                                      save_household_keys)
from .util import _ensure_db

ARMED_AT = "2026-01-05T12:00:00+00:00"
MEMBER = {"email": "member@example.dev", "role": "member",
          "joined": "2026-01-04T00:00:00+00:00"}


class ClearedHoldLeavesNoRecord(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""})
        self._env.start()
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        email = f"chl-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.client.post("/api/signup", data={
            "email": email, "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            self.uid = admin.execute(
                "SELECT id FROM users WHERE lower(email)=lower(%s)",
                (email,)).fetchone()["id"]
        finally:
            admin.close()

    def tearDown(self):
        self._env.stop()

    def _with_conn(self, fn, *a, **kw):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return fn(conn, *a, **kw)
        finally:
            conn.close()

    def _cfg(self):
        return self._with_conn(budget.load_config)

    def _arm(self, plaid=True, mx=False, **extra):
        self._with_conn(save_household_keys, plaid=plaid, mx=mx)
        self._with_conn(arm_hold, at=ARMED_AT, **extra)

    def _clear(self, provider):
        with mock.patch("oikonome.sync.mx.release_tenant_user",
                        return_value="deleted"):
            r = self.client.delete(f"/api/accounts/{provider}/keys")
        self.assertEqual(r.status_code, 200, r.text)

    def _resave(self, plaid=True, mx=False):
        # the owner saves the same client again, after the rotation, as
        # every key door does: with its saved-at stamp
        def save(conn):
            save_household_keys(conn, plaid=plaid, mx=mx)
            with budget.config_txn(conn) as cfg:
                for p, on in (("plaid", plaid), ("mx", mx)):
                    if on:
                        cfg[f"{p}_keys_saved_at"] = dt.datetime.now(
                            dt.timezone.utc).isoformat()
        self._with_conn(save)

    def _note_rotation_removal(self):
        admin = tenancy.admin_connect()
        try:
            op.note_removed_members(admin, self.uid, [dict(MEMBER)],
                                    reason=op.ROTATION)
        finally:
            admin.close()

    def test_clearing_the_keys_a_hold_alone_named_removes_the_record(self):
        for provider, kw in (("plaid", {"plaid": True, "mx": False}),
                             ("mx", {"plaid": False, "mx": True})):
            with self.subTest(provider=provider):
                self._arm(**kw)
                self._clear(provider)
                self.assertNotIn(op.KEY, self._cfg())

    def test_a_later_rotation_shows_its_own_date_and_reason(self):
        self._arm()
        self._clear("plaid")
        self._note_rotation_removal()
        p = op.paused(self._cfg())
        self.assertEqual(p["reason"], op.ROTATION)
        self.assertNotEqual(p["at"], ARMED_AT)
        self.assertEqual([m["email"] for m in p["removed_members"]],
                         [MEMBER["email"]])
        self.assertNotIn("aggregator_clients", self._cfg()[op.KEY])

    def test_a_rotation_never_merges_into_a_record_left_naming_nothing(self):
        # keys cleared by a path that does not settle the record (an
        # older instance, a restore): the raw record still names the
        # client, but holds nothing
        self._arm()

        def forget(conn):
            with budget.config_txn(conn) as cfg:
                for k in ("plaid_client_id", "plaid_secret", "plaid_env"):
                    cfg.pop(k, None)
        self._with_conn(forget)
        self._note_rotation_removal()
        raw = self._cfg()[op.KEY]
        self.assertEqual(raw["reason"], op.ROTATION)
        self.assertNotEqual(raw["at"], ARMED_AT)
        self.assertNotIn("aggregator_clients", raw)
        self._resave()
        self.assertFalse(op.links_held(self._cfg(), "plaid"))

    def test_saving_the_same_client_after_a_clear_is_not_held(self):
        self._arm()
        self._clear("plaid")
        self._resave()
        cfg = self._cfg()
        self.assertFalse(op.links_held(cfg, "plaid"))
        self.assertIsNone(op.paused(cfg))

    def test_a_record_kept_by_another_pause_stops_naming_the_cleared_client(self):
        self._arm(llm=True)
        self._clear("plaid")
        raw = self._cfg()[op.KEY]
        self.assertFalse(raw["aggregator"])
        self.assertNotIn("aggregator_clients", raw)
        self.assertEqual(raw["at"], ARMED_AT)       # still the same pause
        self._resave()
        cfg = self._cfg()
        self.assertFalse(op.links_held(cfg, "plaid"))
        self.assertTrue(op.paused(cfg)["llm"])

    def test_clearing_one_provider_keeps_the_other_held(self):
        self._arm(plaid=True, mx=True)
        self._clear("plaid")
        cfg = self._cfg()
        self.assertFalse(op.links_held(cfg, "plaid"))
        self.assertTrue(op.links_held(cfg, "mx"))
        self.assertEqual(cfg[op.KEY]["aggregator_clients"], {"mx": MX_CLIENT})

    def test_an_unnamed_record_is_pinned_to_what_it_held_when_one_is_cleared(self):
        # a record from before the clients were named holds whatever is
        # stored; once the owner clears one provider, a client saved for
        # it later is not held by that record
        self._arm(plaid=True, mx=True, aggregator_clients=None)

        def unname(conn):
            with budget.config_txn(conn) as cfg:
                cfg[op.KEY].pop("aggregator_clients", None)
        self._with_conn(unname)
        self._clear("plaid")
        self.assertEqual(self._cfg()[op.KEY]["aggregator_clients"],
                         {"mx": MX_CLIENT})

        def other_plaid(conn):
            with budget.config_txn(conn) as cfg:
                cfg["plaid_client_id"] = PLAID_CLIENT
        self._with_conn(other_plaid)
        cfg = self._cfg()
        self.assertFalse(op.links_held(cfg, "plaid"))
        self.assertTrue(op.links_held(cfg, "mx"))


class SavedAfterArming(unittest.TestCase):
    """The same client id, saved again after the record was armed, is the
    owner's: its saved-at stamp is later than the record."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def _cfg(self, saved_at):
        cfg = {"plaid_client_id": PLAID_CLIENT,
               op.KEY: {"at": ARMED_AT, "reason": op.RECOVERY,
                        "aggregator": True,
                        "aggregator_clients": {"plaid": PLAID_CLIENT}}}
        if saved_at is not None:
            cfg["plaid_keys_saved_at"] = saved_at
        return cfg

    def test_a_stamp_after_the_record_is_not_held(self):
        self.assertFalse(op.links_held(
            self._cfg("2026-01-06T09:00:00+00:00"), "plaid"))

    def test_a_stamp_before_the_record_or_none_is_held(self):
        for stamp in ("2025-12-01T09:00:00+00:00", None, "not a time"):
            with self.subTest(stamp=stamp):
                self.assertTrue(op.links_held(self._cfg(stamp), "plaid"))


class HoldSentenceNamesEveryRotation(unittest.TestCase):
    """A login-email change arms the hold with reason "rotation" too, so
    the sentence a held door answers with must name it; a reader whose
    only change was the address would otherwise not recognise the cause."""

    def test_a_rotation_hold_names_the_login_email(self):
        cfg = {op.KEY: {"reason": op.ROTATION, "aggregator": True}}
        self.assertIn("your password, login email or second factor changed",
                      op.hold_message(cfg, "plaid"))


if __name__ == "__main__":
    unittest.main()
