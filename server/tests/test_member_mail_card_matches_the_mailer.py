"""A member's daily-email card says they receive it exactly when the mailer
would mail them.

The card is worked out on the client from what the settings read and
/api/me tell a member; the mailer decides from the stored list, each entry
split into its mailboxes, and each mailbox's (or a single-mailbox entry's)
invitation answer. A member does not see the roster, so every way an entry
can be stored has to reach the card as the same verdict the mailer reaches.
Here the real reads, the real client functions (web and mobile, run under
node) and the real recipient function are put side by side for each stored
shape; nothing in between is written by hand."""

import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

import oikonome
from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.jobs import worker
from oikonome.notify import email_prefs, outbound_pause, recipient_invites

from .util import _ensure_db

PW = "correct-horse-battery"
MEMBER_PW = "family-member-pass"

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_CLIENTS = {"web": _APP / "webapp" / "src" / "recipients.ts",
            "mobile": _APP / "mobile" / "src" / "lib" / "pure.ts"}

# The card's inputs, taken from the two reads the way both settings screens
# pass them to myDailyLine.
_RUNNER = """
const reads = JSON.parse(process.argv[1]);
const out = {};
for (const [side, url] of Object.entries(reads.clients)) {
  const m = await import(url);
  const d = reads.me.daily_email, sv = reads.settings;
  out[side] = m.myDailyLine({
    me: reads.me.email ?? "", on: d.on, muted: d.muted, prefs: d.prefs,
    verified: reads.me.verified, hosted: reads.me.hosted,
    recipients: sv.email_recipients,
    listConfigured: sv.email_list_configured, onList: sv.email_on_list,
    status: sv.email_recipient_status });
}
console.log(JSON.stringify(out));
"""


class MemberMailCardMatchesTheMailer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        for p in _CLIENTS.values():
            if not p.exists():
                raise unittest.SkipTest(f"{p} not present")
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not installed")
        import oikonome.web.app as appmod
        from oikonome.web import security
        security._limiter._hits.clear()
        appmod.DEV_MODE = True
        cls.owner = TestClient(appmod.app)
        r = cls.owner.post("/api/signup", data={
            "email": f"own-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code == 200, r.text
        cls.owner.post("/api/auth/elevate", json={"password": PW})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        r = cls.owner.post("/api/invites", json={
            "label": "partner", "role": "member", "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        cls.member = TestClient(appmod.app)
        cls.me = f"member-{uuid.uuid4().hex[:8]}@example.dev"
        got = cls.member.post("/api/invite/claim", json={
            "token": token, "email": cls.me, "password": MEMBER_PW})
        assert got.status_code == 200, got.text
        cls.aunt = f"aunt-{uuid.uuid4().hex[:6]}@example.dev"

    def _arrange(self, stored, answers, more=None):
        """Store the list, then record each (address, answer) pair as the
        invitation doors would: `invited`, `accepted` or `declined`.
        `more` edits the stored settings further (a mute, a hold)."""
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM recipient_invites WHERE tenant_id=%s",
                          (self.tid,))
        finally:
            admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg.pop("email_muted", None)
                cfg.pop(email_prefs.KEY, None)
                cfg.pop(outbound_pause.KEY, None)
                if more:
                    more(cfg)
                if stored is None:
                    cfg.pop("email_recipients", None)
                else:
                    cfg["email_recipients"] = stored
        finally:
            conn.close()
        for addr, answer in answers:
            token = recipient_invites.invite(self.tid, addr)
            self.assertTrue(token, addr)
            if answer == "accepted":
                self.assertIsNotNone(recipient_invites.accept(token))
            elif answer == "declined":
                self.assertIsNotNone(recipient_invites.decline(token))

    def _mailer_mails_me(self) -> bool:
        conn = tenancy.tenant_connect(self.tid)
        try:
            mailed = worker._recipient_lists(conn, self.tid, "daily")[1]
        finally:
            conn.close()
        return self.me.lower() in {m.lower() for m in mailed}

    def _cards(self) -> dict:
        settings = self.member.get("/api/settings")
        me = self.member.get("/api/me")
        self.assertEqual(settings.status_code, 200, settings.text)
        self.assertEqual(me.status_code, 200, me.text)
        reads = {"settings": settings.json(), "me": me.json(),
                 "clients": {k: p.as_uri() for k, p in _CLIENTS.items()}}
        self.assertTrue(reads["me"]["daily_email"]["on"])
        r = subprocess.run(
            [self.node, "--experimental-strip-types", "--input-type=module",
             "-e", _RUNNER, json.dumps(reads)],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0 and "bad option" in (r.stderr or ""):
            self.skipTest("node too old for --experimental-strip-types")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        return json.loads(r.stdout)

    def test_the_card_says_what_the_mailer_does_for_every_stored_shape(self):
        me, aunt = self.me, self.aunt
        cases = {
            "plain entry, accepted": ([me], [(me, "accepted")]),
            "plain entry, invited not accepted": ([me], [(me, "invited")]),
            "plain entry, declined": ([me], [(me, "declined")]),
            "joined entry, accepted": ([f"{aunt}; {me.upper()}"],
                                       [(me, "accepted")]),
            "joined entry, invited not accepted": ([f"{aunt}; {me}"],
                                                   [(me, "invited")]),
            "display form, accepted": ([f"Kid <{me}>"], [(me, "accepted")]),
            "display form, consent recorded on the entry": (
                [f"Kid <{me}>"], [(f"Kid <{me}>", "accepted")]),
            "a string, not a list": (f"{aunt},{me}", [(me, "accepted")]),
            "login email in another case": ([me.upper()],
                                            [(me, "accepted")]),
            "not on a non-empty list": ([aunt], [(aunt, "accepted")]),
            "nothing configured": (None, []),
            # the owner's rotation holds the member, whose consent is
            # recorded on the display-form entry, not on the mailbox
            "held by a rotation, consent on the entry": (
                [f"Kid <{me}>"], [(f"Kid <{me}>", "accepted")],
                lambda cfg: cfg.__setitem__(outbound_pause.KEY, {
                    "at": "2031-01-01T00:00:00Z", "recipients": [me]}),
                r"Held"),
            "muted by the member": (
                [me], [(me, "accepted")],
                lambda cfg: cfg.__setitem__("email_muted", [me]), r"muted"),
            "the daily kind turned off": (
                [f"{aunt}; {me}"], [(me, "accepted")],
                lambda cfg: email_prefs.set_pref(cfg, me, "daily", False,
                                                 cap=100),
                r"daily email off"),
        }
        for name, (stored, answers, *rest) in cases.items():
            more, text = (rest + [None, None])[:2]
            with self.subTest(name):
                self._arrange(stored, answers, more)
                truth = self._mailer_mails_me()
                for side, card in self._cards().items():
                    self.assertEqual(card["receiving"], truth,
                                     f"{side}: {card['text']}")
                    if text:
                        self.assertRegex(card["text"], text, side)

    # ---- what a member's read and save cost the connection pools ----

    @contextlib.contextmanager
    def _counting_tenant_connections(self):
        state = {"open": 0, "peak": 0}
        real = tenancy.tenant_connect

        def counted(*a, **kw):
            c = real(*a, **kw)
            state["open"] += 1
            state["peak"] = max(state["peak"], state["open"])
            close = c.close

            def closed(*ca, **ckw):
                if not getattr(c, "_counted_closed", False):
                    c._counted_closed = True
                    state["open"] -= 1
                return close(*ca, **ckw)
            c.close = closed
            return c
        with mock.patch.object(tenancy, "tenant_connect", counted):
            yield state

    def _on_the_list_by_a_joined_entry(self):
        self._arrange([f"{self.aunt}; {self.me}"], [(self.me, "accepted")])

    def test_a_member_read_holds_one_tenant_connection(self):
        self._on_the_list_by_a_joined_entry()
        with self._counting_tenant_connections() as n:
            r = self.member.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["email_recipient_status"]), 1)
        self.assertEqual(n["peak"], 1)

    def test_a_member_save_holds_one_tenant_connection(self):
        self._on_the_list_by_a_joined_entry()
        with self._counting_tenant_connections() as n:
            r = self.member.post("/api/settings", json={"food_monthly": 411})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["email_recipient_status"]), 1)
        self.assertEqual(n["peak"], 1)

    def test_a_member_save_answers_on_a_one_connection_pool(self):
        self._on_the_list_by_a_joined_entry()
        saved = dict(tenancy._pools)
        saved_timeout = tenancy.TENANT_POOL_TIMEOUT
        tenancy._pools.clear()
        tenancy.TENANT_POOL_TIMEOUT = 1.0
        try:
            with mock.patch.dict(os.environ, {"OIKONOME_POOL_MAX": "1"}):
                r = self.member.post("/api/settings",
                                     json={"food_monthly": 412})
        finally:
            for p in tenancy._pools.values():
                p.close()
            tenancy._pools.clear()
            tenancy._pools.update(saved)
            tenancy.TENANT_POOL_TIMEOUT = saved_timeout
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_failed_admin_connect_leaves_the_member_read_whole(self):
        self._on_the_list_by_a_joined_entry()

        def refused(*a, **kw):
            raise RuntimeError("admin connect refused")
        with mock.patch.object(tenancy, "admin_connect", refused):
            r = self.member.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        got = r.json()
        self.assertEqual(got["email_recipient_status"], [])
        # with no row to read, the card falls back as on an older server
        self.assertNotIn("email_on_list", got)


if __name__ == "__main__":
    unittest.main()
