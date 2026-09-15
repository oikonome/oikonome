"""The unsubscribe link at the foot of every household email.

One link, one effect: the address a scheduled mail reached can stop ALL of
them — daily, weekly, monthly, yearly and the shared alerts — by landing
itself in the household's `email_muted`, the same per-person opt-out the
Settings page shows and can undo. The contract these tests pin:

  * the token is bound to (tenant, address) and signed — a tampered or
   foreign token does nothing;
  * the emailed link only PEEKS on GET (a mail scanner prefetching every
   link must not silence anyone); the mute is the POST;
  * RFC 8058 one-click — a client's own Unsubscribe button — acts too;
  * muting via the link is exactly `email_muted`, case-insensitive, and
   does not touch anyone else on the list;
  * every household send carries the footer and the List-Unsubscribe
   headers, once per recipient with THAT recipient's token; a LAN base
   that is not mailable gets a linkless footer, never a dead link.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.engine import budget
from oikonome.jobs import worker
from oikonome.notify import unsubscribe as unsub
from oikonome.web import report

from .util import _ensure_db, make_db, write_config


def _tenant():
    conn = make_db()
    tid = str(conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
    return conn, tid


class TokenTests(unittest.TestCase):

    def test_round_trip_normalises_the_address(self):
        tid = str(uuid.uuid4())
        tok = unsub.token(tid, "  Someone@Example.DEV ")
        self.assertEqual(unsub.parse(tok), (tid, "someone@example.dev"))

    def test_tampering_is_rejected(self):
        tid = str(uuid.uuid4())
        tok = unsub.token(tid, "a@example.dev")
        payload, sig = tok.split(".")
        other = unsub.token(tid, "b@example.dev").split(".")[0]
        for bad in ("", "nope", payload, other + "." + sig,
                    payload + "." + sig[:-2] + "AA", tok + "x"):
            self.assertIsNone(unsub.parse(bad), bad)

    def test_key_follows_the_master_key(self):
        tid = str(uuid.uuid4())
        with mock.patch.dict(os.environ, {"OIKONOME_MASTER_KEY": "k-one"}):
            tok = unsub.token(tid, "a@example.dev")
            self.assertIsNotNone(unsub.parse(tok))
        with mock.patch.dict(os.environ, {"OIKONOME_MASTER_KEY": "k-two"}):
            self.assertIsNone(unsub.parse(tok),
                              "a token from another install verified here")

    def test_linkless_footer_when_base_is_not_mailable(self):
        tid = str(uuid.uuid4())
        with mock.patch.dict(os.environ,
                             {"OIKONOME_BASE_URL": "http://192.168.1.50:8042"}):
            plain, html, url = unsub.footer(tid, "a@example.dev")
        self.assertEqual(url, "")
        self.assertNotIn("http", html)
        self.assertIn("Settings", plain)

    def test_decorate_keeps_the_footer_inside_the_mail_wrapper(self):
        tid = str(uuid.uuid4())
        with mock.patch.dict(os.environ,
                             {"OIKONOME_BASE_URL": "https://app.example.dev"}):
            plain, html, url = unsub.decorate(
                "body", '<div><div>body</div></div>', tid, "a@example.dev")
        self.assertTrue(url.startswith("https://app.example.dev/unsubscribe?"
                                       "token="))
        self.assertTrue(html.endswith("</p></div></div>"), html)
        self.assertIn(url, html)
        self.assertIn(url, plain)
        self.assertIn("daily, weekly, monthly and yearly", plain)


class MuteTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn, self.tid = _tenant()

    def tearDown(self):
        self.conn.close()

    def _muted(self):
        return [m.lower() for m in
                (budget.load_config(self.conn).get("email_muted") or [])]

    def test_apply_mutes_only_that_address(self):
        write_config(self.conn, email_muted=["other@example.dev"])
        self.assertTrue(unsub.apply(self.tid, "Me@Example.dev"))
        self.assertEqual(sorted(self._muted()),
                         ["me@example.dev", "other@example.dev"])
        # idempotent — a second click does not grow the list
        self.assertTrue(unsub.apply(self.tid, "me@example.dev"))
        self.assertEqual(self._muted().count("me@example.dev"), 1)

    def test_apply_survives_a_scalar_muted_shape(self):
        write_config(self.conn, email_muted="a@example.dev, b@example.dev")
        self.assertTrue(unsub.apply(self.tid, "c@example.dev"))
        self.assertEqual(sorted(self._muted()),
                         ["a@example.dev", "b@example.dev", "c@example.dev"])


class RouteTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)

    def setUp(self):
        self.conn, self.tid = _tenant()
        self.email = f"guest-{uuid.uuid4().hex[:6]}@example.dev"
        self.tok = unsub.token(self.tid, self.email)

    def tearDown(self):
        self.conn.close()

    def _muted(self):
        return [m.lower() for m in
                (budget.load_config(self.conn).get("email_muted") or [])]

    def test_get_only_peeks(self):
        r = self.client.get("/unsubscribe", params={"token": self.tok})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.email, r.text)
        self.assertIn("Unsubscribe", r.text)
        self.assertEqual(self._muted(), [],
                         "a GET (a mail scanner's prefetch) muted someone")

    def test_post_from_the_page_mutes(self):
        r = self.client.post("/unsubscribe", data={"token": self.tok,
                                                   "answer": "unsubscribe"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Unsubscribed", r.text)
        self.assertEqual(self._muted(), [self.email])

    def test_one_click_post_mutes(self):
        # RFC 8058: the client POSTs the header URL with this exact body
        r = self.client.post(f"/unsubscribe?token={self.tok}",
                             data={"List-Unsubscribe": "One-Click"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._muted(), [self.email])

    def test_bad_token_is_refused_on_both_doors(self):
        for fn, kw in ((self.client.get, {"params": {"token": "nope.nope"}}),
                       (self.client.post, {"data": {"token": "nope.nope"}}),
                       (self.client.post, {"data": {}})):
            r = fn("/unsubscribe", **kw)
            self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._muted(), [])

    def test_muted_address_leaves_the_worker_list(self):
        from oikonome.db import tenancy
        from .util import TEST_DB, _admin_dsn
        owner = f"owner-{uuid.uuid4().hex[:6]}@example.dev"
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'h','owner',now())",
                (self.tid, owner))
        finally:
            admin.close()
        self.assertIn(owner, worker._recipients_raw(self.conn, self.tid))
        tok = unsub.token(self.tid, owner.upper())
        self.client.post("/unsubscribe", data={"token": tok})
        self.assertNotIn(owner, worker._recipients_raw(self.conn, self.tid))


class SendTests(unittest.TestCase):
    """send_each decorates per recipient — each copy carries ITS address's
    link and the List-Unsubscribe headers; with no tenant given, nothing."""

    def _capture(self):
        sent = []

        def fake_deliver(msg, *a, **k):
            sent.append(msg)
        return sent, mock.patch.object(report, "_smtp_deliver", fake_deliver)

    def _smtp(self):
        return {"host": "smtp.example.dev", "port": 587, "user": "u",
                "password": "p", "sender": "no-reply@example.dev",
                "configured": True}

    def test_each_copy_carries_its_own_link(self):
        tid = str(uuid.uuid4())
        sent, patch = self._capture()
        with patch, mock.patch.dict(
                os.environ, {"OIKONOME_BASE_URL": "https://app.example.dev"}), \
                mock.patch("oikonome.notify.delivery.failing",
                           return_value=set()), \
                mock.patch("oikonome.notify.delivery.clear"), \
                mock.patch("oikonome.notify.delivery.record_send_failure"):
            out = report.send_each(
                "Oikonome: daily", "plain body",
                '<div><div>html body</div></div>',
                ["a@example.dev", "B@example.dev"], smtp=self._smtp(),
                unsubscribe=tid)
            urls = {w: unsub.url(tid, w) for w in ("a@example.dev",
                                                   "b@example.dev")}
        self.assertTrue(all(r["ok"] for r in out), out)
        self.assertEqual(len(sent), 2)
        for msg, who in zip(sent, ("a@example.dev", "b@example.dev")):
            url = urls[who]
            self.assertEqual(msg["List-Unsubscribe"], f"<{url}>")
            self.assertEqual(msg["List-Unsubscribe-Post"],
                             "List-Unsubscribe=One-Click")
            plain = msg.get_body(("plain",)).get_content()
            html = msg.get_body(("html",)).get_content()
            self.assertIn(url, plain)
            self.assertIn(url, html)
        self.assertNotEqual(sent[0]["List-Unsubscribe"],
                            sent[1]["List-Unsubscribe"])

    def test_transactional_send_carries_nothing(self):
        sent, patch = self._capture()
        with patch, mock.patch.dict(
                os.environ, {"OIKONOME_BASE_URL": "https://app.example.dev"}), \
                mock.patch("oikonome.notify.delivery.failing",
                           return_value=set()), \
                mock.patch("oikonome.notify.delivery.clear"):
            report.send_each("Oikonome test email", "It works",
                             "<p>It works</p>", ["a@example.dev"],
                             smtp=self._smtp())
        self.assertEqual(len(sent), 1)
        self.assertIsNone(sent[0]["List-Unsubscribe"])
        self.assertNotIn("unsubscribe",
                         sent[0].get_body(("plain",)).get_content().lower())


if __name__ == "__main__":
    unittest.main()
