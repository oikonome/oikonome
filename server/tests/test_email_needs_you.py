"""Acting from the daily email — the "Needs you" section that closes it.

The contract these tests pin:

  * the mail's body no longer opens with the alert strip; alerts, proposed
    bills and uncategorized charges close the mail in a section cut PER
    RECIPIENT, and only an address holding an OWNER or MEMBER account gets
    it — a viewer's copy and a guest's copy carry no section and no alerts;
  * every button is a signed statement bound to (tenant, address, one
    action, one object, expected value) with an expiry — tampered, foreign
    or expired tokens do nothing;
  * the emailed link only PEEKS on GET (a mail scanner prefetching every
    link must not approve a bill); the change is the POST;
  * acting requires the address to STILL hold an owner/member account —
    a viewer's token is refused at the door;
  * a stale button (proposal already decided, charge already filed, alert
    already gone) reports that and overwrites nothing;
  * each copy send_each fans out carries its own address's buttons.
"""

import datetime as dt
import os
import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills
from oikonome.jobs import worker
from oikonome.notify import mailact
from oikonome.web import report

from .util import (TEST_DB, TODAY, _admin_dsn, _ensure_db, add_txn, make_db,
                   write_config)

BASE = "https://app.example.dev"


def _tenant():
    conn = make_db()
    tid = str(conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
    return conn, tid


def _user(tid: str, role: str) -> str:
    email = f"{role}-{uuid.uuid4().hex[:6]}@example.dev"
    admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
    try:
        admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role, "
            "verified_at) VALUES (%s,%s,'h',%s,now())", (tid, email, role))
    finally:
        admin.close()
    return email


class TokenTests(unittest.TestCase):

    def test_round_trip_and_normalised_address(self):
        tid = str(uuid.uuid4())
        tok = mailact.token(tid, " Me@Example.DEV ", "bill", "prop:1", "approve")
        got = mailact.parse(tok)
        self.assertEqual((got["tenant_id"], got["email"], got["action"],
                          got["target"], got["value"]),
                         (tid, "me@example.dev", "bill", "prop:1", "approve"))

    def test_tampering_is_rejected(self):
        tid = str(uuid.uuid4())
        tok = mailact.token(tid, "a@example.dev", "bill", "prop:1", "approve")
        payload, sig = tok.split(".")
        other = mailact.token(tid, "a@example.dev", "bill", "prop:1",
                              "reject").split(".")[0]
        for bad in ("", "nope", payload, other + "." + sig,
                    payload + "." + sig[:-2] + "AA", tok + "x"):
            self.assertIsNone(mailact.parse(bad), bad)
            self.assertFalse(mailact.expired(bad), bad)

    def test_expiry(self):
        tid = str(uuid.uuid4())
        minted = time.time() - (mailact.TTL_DAYS + 1) * 86400
        tok = mailact.token(tid, "a@example.dev", "mute", "k", "m", now=minted)
        self.assertIsNone(mailact.parse(tok))
        self.assertTrue(mailact.expired(tok))
        self.assertIsNotNone(mailact.parse(tok, now=minted + 60))

    def test_key_follows_the_master_key(self):
        tid = str(uuid.uuid4())
        with mock.patch.dict(os.environ, {"OIKONOME_MASTER_KEY": "k-one"}):
            tok = mailact.token(tid, "a@example.dev", "cat", "t1", "MEDICAL")
            self.assertIsNotNone(mailact.parse(tok))
        with mock.patch.dict(os.environ, {"OIKONOME_MASTER_KEY": "k-two"}):
            self.assertIsNone(mailact.parse(tok),
                              "a token from another install verified here")


class _Household(unittest.TestCase):
    """A budgeted tenant with one pending proposal, one uncategorized
    charge in the recent window, an owner, a member, a viewer and a guest
    address; the gathered verdict carries the 'proposals' alert."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn, self.tid = _tenant()
        write_config(self.conn, today_view="detail")
        self.owner = _user(self.tid, "owner")
        self.member = _user(self.tid, "member")
        self.viewer = _user(self.tid, "viewer")
        self.guest = f"guest-{uuid.uuid4().hex[:6]}@example.dev"
        self.pid = f"prop:{uuid.uuid4().hex[:12]}"
        bills._insert_proposal(
            self.conn, pid=self.pid, kind="add", payee="Stream Co",
            amount=15.49, frequency="MONTHLY", interval=1,
            next_due=TODAY + dt.timedelta(days=9),
            evidence={"hits": 4, "cycle_days": 30})
        add_txn(self.conn, TODAY, 42.50, "SAFEWAY", primary="FOOD_AND_DRINK")
        self.txn = add_txn(self.conn, TODAY, 19.17, "CORNER STAND",
                           primary=None, txn_id=f"t-{uuid.uuid4().hex[:8]}")
        self.env = mock.patch.dict(os.environ, {"OIKONOME_BASE_URL": BASE})
        self.env.start()
        self.d = report.gather(self.conn, TODAY)
        self.acts = mailact.gather(self.conn, self.d)

    def tearDown(self):
        self.env.stop()
        self.conn.close()

    def _alert(self):
        return next(a for a in self.d["alerts"] if a["kind"] == "proposals")


class SectionTests(_Household):

    def test_mail_body_no_longer_opens_with_alerts(self):
        subject, plain, html = report.build(self.d)
        msg = self._alert()["message"]
        self.assertNotIn(msg, html)
        self.assertNotIn(msg, plain)
        self.assertNotIn("Needs you", html)

    def test_gather_finds_the_work(self):
        self.assertEqual([p["pid"] for p in self.acts["proposals"]], [self.pid])
        p = self.acts["proposals"][0]
        self.assertEqual((p["yes"], p["no"]), ("Confirm bill", "Not a bill"))
        self.assertEqual([u["txn_id"] for u in self.acts["uncategorized"]],
                         [self.txn])
        opts = self.acts["uncategorized"][0]["options"]
        self.assertEqual(len(opts), mailact.MAX_OPTIONS)
        self.assertIn(("FOOD_AND_DRINK", "FOOD AND DRINK"), opts)
        self.assertNotIn("?", [k for k, _ in opts])
        self.assertTrue(any(a["kind"] == "proposals" for a in self.acts["alerts"]))

    def test_owner_copy_carries_buttons_signed_for_that_address(self):
        plain, html = mailact.render(self.acts, self.tid, self.owner.upper())
        self.assertIn("Needs you", html)
        self.assertIn(self._alert()["message"], html)
        self.assertIn("mute ✕", html)
        self.assertIn("Confirm bill", html)
        self.assertIn("FOOD AND DRINK", html)
        yes = mailact.url(BASE, self.tid, self.owner, "bill", self.pid, "approve")
        self.assertIn(yes, html)
        self.assertIn(yes, plain)
        self.assertIn("Needs you", plain)
        for forbidden in ("<script", "<svg", "class=", "var(--", "<form"):
            self.assertNotIn(forbidden, html, forbidden)
        self.assertIn(f"{BASE}/app/transactions?cat=%3F", html)

    def test_section_is_for_owners_and_members_only(self):
        personal = worker._needs_you(self.conn, self.tid, self.d)
        for who in (self.owner, self.member):
            plain, html = personal(who)
            self.assertIn("Needs you", html, who)
        for who in (self.viewer, self.guest):
            self.assertEqual(personal(who), ("", ""), who)

    def test_linkless_install_keeps_the_alerts_as_text(self):
        with mock.patch.dict(os.environ,
                             {"OIKONOME_BASE_URL": "http://192.168.1.50:8042"}):
            plain, html = mailact.render(self.acts, self.tid, self.owner)
        self.assertIn(self._alert()["message"], html)
        self.assertNotIn("Confirm bill", html)
        self.assertNotIn("http", html)
        self.assertNotIn("mute", html)

    def test_send_each_cuts_a_copy_per_address(self):
        sent = []

        def fake_deliver(msg, *a, **k):
            sent.append(msg)
        smtp = {"host": "smtp.example.dev", "port": 587, "user": "u",
                "password": "p", "sender": "no-reply@example.dev",
                "configured": True}
        subject, plain, html = report.build(self.d)
        # the token stamps its expiry from the clock, to the second; pinned
        # so the mail and the URL rebuilt below are signed at one instant
        now = time.time()
        pin = mock.patch.object(mailact.time, "time", return_value=now)
        pin.start()
        self.addCleanup(pin.stop)
        personal = worker._needs_you(self.conn, self.tid, self.d)
        with mock.patch.object(report, "_smtp_deliver", fake_deliver), \
                mock.patch("oikonome.notify.delivery.failing",
                           return_value=set()), \
                mock.patch("oikonome.notify.delivery.clear"), \
                mock.patch("oikonome.notify.delivery.record_send_failure"):
            out = report.send_each(subject, plain, html,
                                   [self.owner, self.viewer, self.guest],
                                   smtp=smtp, unsubscribe=self.tid,
                                   personal=personal)
        self.assertTrue(all(r["ok"] for r in out), out)
        bodies = {m["To"]: m.get_body(("html",)).get_content() for m in sent}
        own = bodies[self.owner]
        self.assertIn("Needs you", own)
        self.assertIn(mailact.url(BASE, self.tid, self.owner, "bill",
                                  self.pid, "approve"), own)
        # the section sits INSIDE the wrapper and BEFORE the footer
        self.assertLess(own.index("Needs you"), own.index("Unsubscribe"))
        self.assertTrue(own.rstrip().endswith("</div></div>"))
        for who in (self.viewer, self.guest):
            self.assertNotIn("Needs you", bodies[who], who)
            self.assertNotIn(self._alert()["message"], bodies[who], who)
            self.assertIn("Unsubscribe", bodies[who], who)


class _Routes(_Household):
    """The household plus a client for the /act page."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import oikonome.web.app as appmod
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)

    def _tok(self, who, action, target, value):
        return mailact.token(self.tid, who, action, target, value)

    def _status(self):
        return self.conn.execute(
            "SELECT status FROM bill_proposals WHERE id=%s",
            (self.pid,)).fetchone()["status"]


class RouteTests(_Routes):

    def test_get_only_peeks(self):
        tok = self._tok(self.owner, "bill", self.pid, "approve")
        r = self.client.get("/act", params={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Stream Co", r.text)
        self.assertIn("monthly bill", r.text)
        self.assertEqual(self._status(), "pending",
                         "a GET (a mail scanner's prefetch) approved a bill")

    def test_post_approves_then_reports_already(self):
        tok = self._tok(self.member, "bill", self.pid, "approve")
        r = self.client.post("/act", data={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("now tracked", r.text)
        self.assertEqual(self._status(), "approved")
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM bills WHERE payee='Stream Co'").fetchone())
        # the reject button from the same mail must not undo the decision
        r = self.client.post("/act", data={"token": self._tok(
            self.member, "bill", self.pid, "reject")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("already", r.text.lower())
        self.assertEqual(self._status(), "approved")

    def test_post_rejects(self):
        tok = self._tok(self.owner, "bill", self.pid, "reject")
        r = self.client.post("/act", data={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._status(), "rejected")

    def test_post_files_the_charge_this_row_only(self):
        tok = self._tok(self.owner, "cat", self.txn, "FOOD_AND_DRINK")
        r = self.client.get("/act", params={"token": tok})
        self.assertIn("FOOD AND DRINK", r.text)
        r = self.client.post("/act", data={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        row = self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (self.txn,)).fetchone()
        self.assertEqual(row["category_override"], "FOOD_AND_DRINK")
        self.assertFalse(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE merchant ILIKE "
            "'%%corner stand%%'").fetchone(), "an email tap wrote a rule")
        r = self.client.post("/act", data={"token": tok})
        self.assertIn("already", r.text.lower())

    def test_post_mutes_the_alert(self):
        a = self._alert()
        tok = self._tok(self.owner, "mute", a["kind"], a["message"])
        r = self.client.get("/act", params={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Mute", r.text)
        r = self.client.post("/act", data={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Muted", r.text)
        d2 = report.gather(self.conn, TODAY)
        self.assertNotIn(a["message"], [x["message"] for x in d2["alerts"]])
        r = self.client.post("/act", data={"token": tok})
        self.assertIn("already", r.text.lower())

    def test_viewer_and_guest_tokens_are_refused(self):
        for who in (self.viewer, self.guest):
            tok = self._tok(who, "bill", self.pid, "approve")
            for fn, kw in ((self.client.get, {"params": {"token": tok}}),
                           (self.client.post, {"data": {"token": tok}})):
                r = fn("/act", **kw)
                self.assertEqual(r.status_code, 403, (who, r.text))
        self.assertEqual(self._status(), "pending")

    def test_bad_expired_and_foreign_tokens(self):
        old = mailact.token(self.tid, self.owner, "bill", self.pid, "approve",
                            now=time.time() - (mailact.TTL_DAYS + 1) * 86400)
        r = self.client.post("/act", data={"token": old})
        self.assertEqual(r.status_code, 400)
        self.assertIn("expired", r.text)
        for bad in ("nope.nope", ""):
            r = self.client.post("/act", data={"token": bad})
            self.assertEqual(r.status_code, 400, r.text)
        other = mailact.token(str(uuid.uuid4()), self.owner, "bill", self.pid,
                              "approve")
        r = self.client.post("/act", data={"token": other})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self._status(), "pending")


class StandingTests(_Routes):
    """A button in the mail is a door into the household like any other, so
    it must pass the gates every in-app write passes: a frozen household
    (suspended, scheduled for deletion, never confirmed), a household held
    read-only by billing, and a hosted account that has enrolled no second
    factor. A token minted while the household was in good standing must
    not outlive that standing — the mail cannot be recalled once sent."""

    def _set_status(self, status):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute("UPDATE tenants SET status=%s WHERE id=%s",
                          (status, self.tid))
        finally:
            admin.close()

    def _tokens(self):
        a = self._alert()
        return [self._tok(self.owner, "bill", self.pid, "approve"),
                self._tok(self.owner, "bill", self.pid, "reject"),
                self._tok(self.owner, "cat", self.txn, "FOOD_AND_DRINK"),
                self._tok(self.owner, "mute", a["kind"], a["message"])]

    def _untouched(self):
        self.assertEqual(self._status(), "pending")
        self.assertIsNone(self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (self.txn,)).fetchone()["category_override"])
        a = self._alert()
        self.assertFalse(self.conn.execute(
            "SELECT dismissed FROM alerts_log WHERE kind=%s AND message=%s",
            (a["kind"], a["message"])).fetchone()["dismissed"])

    def _refused(self, code):
        for tok in self._tokens():
            r = self.client.get("/act", params={"token": tok})
            self.assertEqual(r.status_code, code, r.text)
            self.assertNotIn('action="/act"', r.text,
                             "the page offered a button that must fail")
            r = self.client.post("/act", data={"token": tok})
            self.assertEqual(r.status_code, code, r.text)
        self._untouched()

    def test_frozen_household_buttons_write_nothing(self):
        import oikonome.web.app as appmod
        for status, (code, _msg, _open) in appmod.LOCKOUT_STATUSES.items():
            with self.subTest(status=status):
                self._set_status(status)
                self._refused(code)
        self._set_status("active")

    def test_read_only_household_buttons_write_nothing(self):
        from oikonome import ext
        with mock.patch.object(ext.gate, "write_blocked",
                               lambda tid: str(tid) == self.tid):
            self._refused(402)

    def test_hosted_account_without_a_second_factor_cannot_act_from_mail(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            self._refused(403)
            admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
            try:
                admin.execute("UPDATE users SET second_factor_waived=true "
                              "WHERE tenant_id=%s AND email=%s",
                              (self.tid, self.owner))
            finally:
                admin.close()
            r = self.client.post("/act", data={"token": self._tok(
                self.owner, "bill", self.pid, "approve")})
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._status(), "approved")


class FileUnderStaleTests(_Routes):
    """The 'file under' button was minted for an UNCATEGORIZED charge. Once
    anyone has filed it — the other address on the mail, the app, a rule —
    the button is stale: the page names the category the charge is under
    now and changes nothing, the way the bill button refuses a proposal
    that was already decided. Comparing only against the button's own
    value would let a week-old mailed guess silently replace a later,
    deliberate decision."""

    def _cat(self):
        return self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (self.txn,)).fetchone()["category_override"]

    def test_file_under_refuses_a_charge_filed_since_the_mail(self):
        from oikonome.web import data
        tok = self._tok(self.owner, "cat", self.txn, "FOOD_AND_DRINK")
        data.set_category(self.conn, self.txn, "TRAVEL", scope="one")
        r = self.client.get("/act", params={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("now filed under TRAVEL", r.text)
        self.assertNotIn('action="/act"', r.text)
        r = self.client.post("/act", data={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("now filed under TRAVEL", r.text)
        self.assertEqual(self._cat(), "TRAVEL",
                         "a mailed guess overwrote a later decision")

    def test_file_under_reports_a_charge_split_since_the_mail(self):
        tok = self._tok(self.owner, "cat", self.txn, "FOOD_AND_DRINK")
        self.conn.execute(
            "INSERT INTO transaction_splits (txn_id, line, category, amount) "
            "VALUES (%s,1,'FOOD_AND_DRINK',10.00), (%s,2,'TRAVEL',9.17)",
            (self.txn, self.txn))
        for fn, kw in ((self.client.get, {"params": {"token": tok}}),
                       (self.client.post, {"data": {"token": tok}})):
            r = fn("/act", **kw)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("split", r.text)
            self.assertNotIn("is filed under", r.text)
        self.assertIsNone(self._cat())

    def test_file_under_cannot_be_replayed_after_the_charge_is_cleared(self):
        """A used button is spent: after the charge is filed from the mail
        and someone clears it again, the same link must not re-file it."""
        tok = self._tok(self.owner, "cat", self.txn, "FOOD_AND_DRINK")
        r = self.client.post("/act", data={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        self.conn.execute("DELETE FROM manual_categories "
                          "WHERE transaction_id=%s", (self.txn,))
        self.conn.execute("UPDATE transactions SET category_override=NULL, "
                          "override_source=NULL WHERE id=%s", (self.txn,))
        r = self.client.post("/act", data={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("changed after the email went out", r.text)
        self.assertIsNone(self._cat(), "a spent button re-filed the charge")


class MuteStaleTests(_Routes):
    """A mute button is spent once the alert's mute has changed since the
    mail went out. Muting from the mail and then restoring the alert in
    the app is a decision to see it again; replaying the week-old link
    must not silently hide it a second time."""

    def _dismissed(self, a):
        return self.conn.execute(
            "SELECT dismissed FROM alerts_log WHERE kind=%s AND message=%s",
            (a["kind"], a["message"])).fetchone()["dismissed"]

    def test_mute_cannot_be_replayed_after_the_alert_is_restored(self):
        from oikonome.engine import alerts
        a = self._alert()
        tok = self._tok(self.owner, "mute", a["kind"], a["message"])
        r = self.client.post("/act", data={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._dismissed(a), 1)
        alerts.restore(self.conn, a["kind"], a["message"])
        r = self.client.get("/act", params={"token": tok})
        self.assertNotIn('action="/act"', r.text)
        r = self.client.post("/act", data={"token": tok})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("changed after the email went out", r.text)
        self.assertEqual(self._dismissed(a), 0,
                         "a spent mute link hid a restored alert again")


if __name__ == "__main__":
    unittest.main()
