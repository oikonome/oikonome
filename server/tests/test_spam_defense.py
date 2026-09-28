"""Spam defense for public email intake (signup, and any intake form an add-on adds).

Local merged-blocklist check at the form (instant, no network), a budgeted +
cached live check (StopForumSpam email lookup + DNS null-route), major-provider
safelist → address-only blocks, silent generic-success drop (no enumeration),
operator decline (polite email) / decline-as-spam (silent + domain block), an
auto-updating public feed, and a re-scan of pending requests. Live signals are
off under DEV_MODE unless OIKONOME_SPAM_API is set, so this suite is hermetic —
the live path is exercised only via monkeypatch.
"""

import os
import unittest

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import spamdefense

from .util import _ensure_db

TOKEN = "test-admin-token-" + "z" * 32

_SPAM_TABLES = ("spam_domains_public", "spam_domains_blocked",
                "spam_addresses_blocked", "spam_reputation_cache", "spam_drops")


def _admin():
    return tenancy.admin_connect()


def _wipe():
    admin = _admin()
    try:
        for t in _SPAM_TABLES:
            admin.execute(f"TRUNCATE {t}")
        admin.execute("UPDATE blocklist_meta SET public_count=0, "
                      "last_fetched_at=NULL WHERE id")
    finally:
        admin.close()


def _rows(sql, params=()):
    admin = _admin()
    try:
        return admin.execute(sql, params).fetchall()
    finally:
        admin.close()


class SpamModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def setUp(self):
        os.environ["OIKONOME_SPAM_DEFENSE"] = "1"
        os.environ.pop("OIKONOME_SPAM_API", None)
        _wipe()

    def tearDown(self):
        for k in ("OIKONOME_SPAM_DEFENSE", "OIKONOME_SPAM_API",
                  "OIKONOME_HOSTED"):
            os.environ.pop(k, None)

    # --- normalization ---
    def test_domain_of(self):
        self.assertEqual(spamdefense.domain_of("A@Example.COM"), "example.com")
        self.assertEqual(spamdefense.domain_of("x@sub.d.co."), "sub.d.co")
        self.assertIsNone(spamdefense.domain_of("no-at-sign"))
        self.assertIsNone(spamdefense.domain_of("x@localhost"))

    # --- enforcement toggle ---
    def test_enforcement_off_is_noop(self):
        os.environ.pop("OIKONOME_SPAM_DEFENSE", None)   # and no HOSTED
        admin = _admin()
        try:
            admin.execute("INSERT INTO spam_domains_public (domain) "
                          "VALUES ('listed.test')")
        finally:
            admin.close()
        self.assertFalse(
            spamdefense.check("request", "a@listed.test")["blocked"])

    # --- local list ---
    def test_public_list_blocks_and_logs_drop(self):
        admin = _admin()
        try:
            admin.execute("INSERT INTO spam_domains_public (domain, sources) "
                          "VALUES ('spammy.test', ARRAY['sfs_toxic'])")
        finally:
            admin.close()
        res = spamdefense.check("request", "bot@spammy.test")
        self.assertTrue(res["blocked"])
        self.assertEqual(res["reason"], "public_list")
        drops = _rows("SELECT domain, surface, reason FROM spam_drops")
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["reason"], "public_list")
        # a public-list hit is also recorded in the operator's blocked view
        self.assertEqual(len(_rows(
            "SELECT 1 FROM spam_domains_blocked WHERE domain='spammy.test' "
            "AND source='auto'")), 1)

    def test_manual_blocked_domain(self):
        admin = _admin()
        try:
            spamdefense.manual_block(admin, "evil.test", added_by="operator")
        finally:
            admin.close()
        self.assertEqual(
            spamdefense.check("request", "x@evil.test")["reason"], "blocked")

    # --- safelist protection ---
    def test_safelist_auto_block_is_address_only(self):
        admin = _admin()
        try:
            spamdefense.auto_block(admin, "gmail.com", "bad@gmail.com",
                                   "test")
        finally:
            admin.close()
        self.assertEqual(
            _rows("SELECT 1 FROM spam_domains_blocked WHERE domain='gmail.com'"),
            [])
        self.assertEqual(len(_rows(
            "SELECT 1 FROM spam_addresses_blocked WHERE email='bad@gmail.com'")),
            1)
        # a different gmail address is NOT blocked; the bad one is
        self.assertFalse(spamdefense.check("request", "ok@gmail.com")["blocked"])
        self.assertEqual(
            spamdefense.check("request", "bad@gmail.com")["reason"], "address")

    # --- live path (monkeypatched) ---
    def test_live_flag_blocks_and_caches(self):
        os.environ["OIKONOME_SPAM_API"] = "on"
        orig_sfs = spamdefense._sfs_lookup
        spamdefense._sfs_lookup = lambda e, ip, t: {
            "blacklisted": 1, "confidence": 99.9, "frequency": 255}
        try:
            res = spamdefense.check("request", "x@fresh-live.test")
        finally:
            spamdefense._sfs_lookup = orig_sfs
        self.assertTrue(res["blocked"])
        self.assertEqual(res["reason"], "live")
        self.assertEqual(len(_rows(
            "SELECT 1 FROM spam_domains_blocked WHERE domain='fresh-live.test'")),
            1)
        self.assertEqual(_rows(
            "SELECT verdict FROM spam_reputation_cache "
            "WHERE domain='fresh-live.test'")[0]["verdict"], "spam")

    def test_live_clean_allows_and_caches(self):
        os.environ["OIKONOME_SPAM_API"] = "on"
        orig_sfs = spamdefense._sfs_lookup
        spamdefense._sfs_lookup = lambda e, ip, t: {
            "blacklisted": 0, "confidence": 0, "frequency": 0}
        try:
            self.assertFalse(
                spamdefense.check("request", "x@clean-live.test")["blocked"])
        finally:
            spamdefense._sfs_lookup = orig_sfs
        self.assertEqual(_rows(
            "SELECT verdict FROM spam_reputation_cache "
            "WHERE domain='clean-live.test'")[0]["verdict"], "clean")

    def test_live_timeout_fails_open(self):
        os.environ["OIKONOME_SPAM_API"] = "on"
        orig_sfs = spamdefense._sfs_lookup
        spamdefense._sfs_lookup = lambda e, ip, t: None   # timeout / API down
        try:
            self.assertFalse(
                spamdefense.check("request", "x@unknown-live.test")["blocked"])
        finally:
            spamdefense._sfs_lookup = orig_sfs
        # unknown is not cached — a later retry can still catch it
        self.assertEqual(_rows(
            "SELECT 1 FROM spam_reputation_cache "
            "WHERE domain='unknown-live.test'"), [])

    def test_live_probe_never_leaks_real_email(self):
        # the live lookup must send a SYNTHETIC probe address, never the
        # user's real email (third-party privacy)
        os.environ["OIKONOME_SPAM_API"] = "on"
        seen = {}
        orig_sfs = spamdefense._sfs_lookup

        def _cap(email, ip, t):
            seen["email"] = email
            return {"blacklisted": 0, "confidence": 0}
        spamdefense._sfs_lookup = _cap
        try:
            spamdefense.check("request", "realperson@novel-probe.test")
        finally:
            spamdefense._sfs_lookup = orig_sfs
        self.assertEqual(seen["email"], "probe@novel-probe.test")
        self.assertNotIn("realperson", seen["email"])

    def test_manual_block_of_safelisted_domain_is_honored(self):
        # an explicit operator block must not be shadowed
        # by the safelist
        admin = _admin()
        try:
            spamdefense.manual_block(admin, "gmail.com", added_by="op")
        finally:
            admin.close()
        self.assertTrue(spamdefense.check("request", "x@gmail.com")["blocked"])
        # a normal gmail address (no block) is still fine
        self.assertFalse(spamdefense.check("request", "someone@yahoo.com")["blocked"])

    # --- refresh ---
    def test_refresh_merges_dedupes_and_filters_safelist(self):
        orig = spamdefense._fetch_list
        spamdefense._fetch_list = lambda url: (
            ["a.test", "dup.test", "gmail.com"] if "disposable" in url
            else ["dup.test", "b.test"])
        try:
            res = spamdefense.refresh_lists()
        finally:
            spamdefense._fetch_list = orig
        self.assertTrue(res["ok"])
        got = {r["domain"] for r in _rows("SELECT domain FROM spam_domains_public")}
        self.assertEqual(got, {"a.test", "b.test", "dup.test"})   # gmail filtered
        self.assertEqual(_rows(
            "SELECT public_count FROM blocklist_meta WHERE id")[0]["public_count"],
            3)

    def test_refresh_failed_fetch_keeps_old_list(self):
        admin = _admin()
        try:
            admin.execute("INSERT INTO spam_domains_public (domain) "
                          "VALUES ('old.test')")
        finally:
            admin.close()

        def _boom(url):
            raise ValueError("network down")
        orig = spamdefense._fetch_list
        spamdefense._fetch_list = _boom
        try:
            res = spamdefense.refresh_lists()
        finally:
            spamdefense._fetch_list = orig
        self.assertFalse(res["ok"])
        self.assertEqual(len(_rows(
            "SELECT 1 FROM spam_domains_public WHERE domain='old.test'")), 1)

    # --- recipient guard (don't waste transactional mail) ---
    def test_recipient_blocked(self):
        admin = _admin()
        try:
            admin.execute("INSERT INTO spam_domains_public (domain) "
                          "VALUES ('nomail.test')")
            admin.execute("INSERT INTO spam_domains_blocked (domain) "
                          "VALUES ('banned.test')")
            admin.execute("INSERT INTO spam_addresses_blocked (email) "
                          "VALUES ('crook@gmail.com')")
        finally:
            admin.close()
        self.assertTrue(spamdefense.recipient_blocked("x@nomail.test"))
        self.assertTrue(spamdefense.recipient_blocked("x@banned.test"))
        self.assertTrue(spamdefense.recipient_blocked("crook@gmail.com"))
        # a normal address, and a safelisted domain, are never blocked
        self.assertFalse(spamdefense.recipient_blocked("real@ok.test"))
        self.assertFalse(spamdefense.recipient_blocked("someone@gmail.com"))
        # off when enforcement is off
        os.environ.pop("OIKONOME_SPAM_DEFENSE", None)
        self.assertFalse(spamdefense.recipient_blocked("x@banned.test"))


class SpamConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        os.environ["OIKONOME_ADMIN_TOKEN"] = TOKEN
        os.environ["OIKONOME_SPAM_DEFENSE"] = "1"
        from oikonome.web import security
        security._limiter._hits.clear()
        _wipe()
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/admin/console/login", data={"token": TOKEN},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def tearDown(self):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)
        os.environ.pop("OIKONOME_SPAM_DEFENSE", None)


    def test_block_and_unblock(self):
        self.client.post("/admin/console/block",
                         data={"value": "ManualBlock.TEST"},
                         follow_redirects=False)
        self.assertEqual(len(_rows(
            "SELECT 1 FROM spam_domains_blocked WHERE domain='manualblock.test'")),
            1)
        self.client.post("/admin/console/unblock",
                         data={"value": "manualblock.test"},
                         follow_redirects=False)
        self.assertEqual(_rows(
            "SELECT 1 FROM spam_domains_blocked WHERE domain='manualblock.test'"),
            [])


if __name__ == "__main__":
    unittest.main()
