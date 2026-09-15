"""Passphrase-sealed connections export/import.

The load-bearing property is master-key INDEPENDENCE: a bundle exported on
one install must re-establish live bank links on another install whose
OIKONOME_MASTER_KEY differs. Proven by rotating the process master key
between export and import and asserting the token decrypts on the far side.
"""

import base64
import json
import os
import unittest
from unittest import mock

from cryptography.fernet import Fernet

from oikonome.db import crypto
from oikonome.engine import budget
from oikonome.sync import base as sync_base
from oikonome.sync import connections_bundle as cb

from .util import make_db, write_config


class ConnectionsBundleTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("OIKONOME_MASTER_KEY", None)

    def _db(self):
        """make_db + guaranteed pool return — leaked connections starve the
        shared pool and PoolTimeout unrelated tests."""
        conn = make_db()
        self.addCleanup(conn.close)
        return conn

    def _seed_source(self, conn):
        """A tenant with the full spread of environment config: Plaid keys,
        an LLM endpoint + key, SMTP mailer, and a live item (fixture 'it1')."""
        write_config(
            conn,
            plaid_client_id="cid-123",
            plaid_secret=crypto.encrypt(conn, "plaid-secret-xyz"),
            plaid_env="production",
            llm_url="https://llm.local/v1/chat/completions",
            llm_model="gemma-3",
            llm_api_key=crypto.encrypt(conn, "llm-key-abc"),
            smtp_host="smtp.example.net", smtp_port=587,
            smtp_user="owner@example.com", smtp_from="owner@example.com",
            smtp_password=crypto.encrypt(conn, "smtp-pw-123"))

    def test_roundtrip_same_key(self):
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        src = self._db()
        self._seed_source(src)
        # fixture seeds item 'it1' with token 'tok-test'
        blob = cb.export_bytes(src, "correct horse battery")
        for secret in (b"tok-test", b"plaid-secret-xyz", b"llm-key-abc",
                       b"smtp-pw-123"):
            self.assertNotIn(secret, blob)            # sealed, not plaintext

        dst = self._db()                               # fresh tenant
        summary = cb.import_bytes(dst, blob, "correct horse battery")
        self.assertEqual(set(summary["config"]), {"plaid", "llm", "smtp"})
        self.assertGreaterEqual(summary["items"], 1)

        cfg = budget.load_config(dst)
        self.assertEqual(cfg["plaid_client_id"], "cid-123")
        self.assertEqual(cfg["llm_model"], "gemma-3")
        self.assertEqual(cfg["smtp_host"], "smtp.example.net")
        self.assertEqual(cfg["smtp_port"], 587)
        self.assertEqual(crypto.decrypt(dst, cfg["plaid_secret"]),
                         "plaid-secret-xyz")
        self.assertEqual(crypto.decrypt(dst, cfg["llm_api_key"]),
                         "llm-key-abc")
        self.assertEqual(crypto.decrypt(dst, cfg["smtp_password"]),
                         "smtp-pw-123")
        self.assertEqual(sync_base.get_access_token(dst, "it1"), "tok-test")

    def test_master_key_independent(self):
        key_a = Fernet.generate_key().decode()
        key_b = Fernet.generate_key().decode()
        self.assertNotEqual(key_a, key_b)

        os.environ["OIKONOME_MASTER_KEY"] = key_a
        src = self._db()
        self._seed_source(src)
        blob = cb.export_bytes(src, "pass-across-boxes")

        # different install → different master key
        os.environ["OIKONOME_MASTER_KEY"] = key_b
        dst = self._db()
        cb.import_bytes(dst, blob, "pass-across-boxes")
        # every secret decrypts cleanly under key B — the whole point
        self.assertEqual(sync_base.get_access_token(dst, "it1"), "tok-test")
        cfg = budget.load_config(dst)
        self.assertEqual(crypto.decrypt(dst, cfg["plaid_secret"]),
                         "plaid-secret-xyz")
        self.assertEqual(crypto.decrypt(dst, cfg["llm_api_key"]),
                         "llm-key-abc")
        self.assertEqual(crypto.decrypt(dst, cfg["smtp_password"]),
                         "smtp-pw-123")

    def test_wrong_passphrase_rejected(self):
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        src = self._db()
        blob = cb.export_bytes(src, "the-right-one")
        dst = self._db()
        with self.assertRaises(ValueError):
            cb.import_bytes(dst, blob, "the-wrong-one")

    def test_not_a_bundle_rejected(self):
        dst = self._db()
        with self.assertRaises(ValueError):
            cb.import_bytes(dst, b"just some bytes", "whatever")

    def test_whitelist_blocks_arbitrary_config(self):
        """A tampered payload can't inject non-credential config keys."""
        dst = self._db()
        summary = cb.apply_payload(dst, {
            "oikonome_config": cb.FORMAT,
            "config": {"smtp_host": "mail.example", "food_monthly": 999999},
            "secrets": {"smtp_password": "pw"},
            "items": [],
        })
        cfg = budget.load_config(dst)
        self.assertEqual(cfg["smtp_host"], "mail.example")   # whitelisted
        self.assertNotEqual(cfg.get("food_monthly"), 999999)  # rejected
        self.assertEqual(summary["config"], ["smtp"])

    def test_hosted_strips_items_from_an_imported_bundle(self):
        """Items ARE credentials (a SimpleFIN item's
        access_token is the bearer URL to bank data) and hosted items share
        the platform's Plaid client — the dedicated link doors refuse BYO on
        hosted, and a bundle must not be the side door around them."""
        from unittest import mock

        from oikonome.sync import connections_bundle as cb
        conn = self._db()
        payload = {"oikonome_config": cb.FORMAT, "config": {}, "secrets": {},
                   "items": [{"id": "sf-x", "aggregator": "simplefin",
                              "institution_name": "Bridge",
                              "access_token":
                                  "https://u:p@bridge.simplefin.org/x"}]}
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            out = cb.apply_payload(conn, payload)
        self.assertEqual(out["items"], 0)
        row = conn.execute("SELECT 1 FROM items WHERE id='sf-x'").fetchone()
        self.assertIsNone(row, "a hosted bundle import planted a BYO item")

    def test_bundle_import_respects_the_institution_cap(self):
        """A connection cap enforced only at the Plaid doors lets a
        hand-written bundle of N items sail past it."""
        from unittest import mock

        from oikonome.sync import base as sync_base
        from oikonome.sync import connections_bundle as cb
        conn = self._db()
        items = [{"id": f"cap-{i}", "aggregator": "simplefin",
                  "institution_name": f"Bank {i}",
                  "access_token": f"https://u:p@bridge.simplefin.org/{i}"}
                 for i in range(4)]
        payload = {"oikonome_config": cb.FORMAT, "config": {}, "secrets": {},
                   "items": items}
        with mock.patch.object(sync_base, "institution_cap", return_value=2):
            with self.assertRaises(ValueError) as ctx:
                cb.apply_payload(conn, payload)
        self.assertIn("cap", str(ctx.exception))
        row = conn.execute("SELECT 1 FROM items WHERE id='cap-0'").fetchone()
        self.assertIsNone(row, "the refused bundle still planted items")
        # under the cap, the same shape applies fine (self-host: cap None)
        out = cb.apply_payload(conn, payload)
        self.assertEqual(out["items"], 4)

    def test_short_passphrase_rejected(self):
        src = self._db()
        with self.assertRaises(ValueError):
            cb.export_bytes(src, "short")


class BundleCarriesNamedBackendsTests(unittest.TestCase):
    """The bundle is advertised as everything a fresh install needs to be a
    working copy, so it must carry the AI setup a tenant actually has. A
    tenant on named backends keeps every endpoint and key under
    `llm_backends`; carrying only the legacy single-endpoint keys would
    leave such a tenant's bundle silently holding no AI configuration.
    """

    def tearDown(self):
        os.environ.pop("OIKONOME_MASTER_KEY", None)

    def _db(self):
        conn = make_db()
        self.addCleanup(conn.close)
        return conn

    def _seed_backends(self, conn):
        write_config(
            conn,
            llm_backends=[
                {"id": "local", "name": "Local", "url": "https://llm.local/v1",
                 "model": "gemma-3",
                 "api_key": crypto.encrypt(conn, "local-key"),
                 "extra_body": crypto.encrypt(conn, '{"enable_thinking":0}')},
                {"id": "cloud", "name": "Cloud", "url": "https://llm.cloud/v1",
                 "model": "big", "vision_model": "big-vision",
                 "api_key": crypto.encrypt(conn, "cloud-key")},
            ],
            llm_roles={"categorize": "local", "assistant": "cloud"})

    def test_backends_survive_a_master_key_change(self):
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        src = self._db()
        self._seed_backends(src)
        blob = cb.export_bytes(src, "across-the-boxes")
        for secret in (b"local-key", b"cloud-key", b"enable_thinking"):
            self.assertNotIn(secret, blob)          # sealed, not plaintext

        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        dst = self._db()
        summary = cb.import_bytes(dst, blob, "across-the-boxes")
        self.assertIn("llm", summary["config"])

        cfg = budget.load_config(dst)
        by_id = {b["id"]: b for b in cfg["llm_backends"]}
        self.assertEqual(set(by_id), {"local", "cloud"})
        self.assertEqual(by_id["local"]["url"], "https://llm.local/v1")
        self.assertEqual(by_id["cloud"]["vision_model"], "big-vision")
        # re-encrypted under the DESTINATION's key — the whole point
        self.assertEqual(crypto.decrypt(dst, by_id["local"]["api_key"]),
                         "local-key")
        self.assertEqual(crypto.decrypt(dst, by_id["local"]["extra_body"]),
                         '{"enable_thinking":0}')
        self.assertEqual(crypto.decrypt(dst, by_id["cloud"]["api_key"]),
                         "cloud-key")
        # routing travels too, or the backends arrive doing nothing
        self.assertEqual(cfg["llm_roles"],
                         {"categorize": "local", "assistant": "cloud"})

    def test_import_keeps_a_backend_the_bundle_never_mentions(self):
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        src = self._db()
        self._seed_backends(src)
        blob = cb.export_bytes(src, "across-the-boxes")

        dst = self._db()
        write_config(dst, llm_backends=[
            {"id": "mine", "name": "Mine", "url": "https://llm.mine/v1",
             "model": "small", "api_key": crypto.encrypt(dst, "my-key")}])
        cb.import_bytes(dst, blob, "across-the-boxes")

        by_id = {b["id"]: b for b in budget.load_config(dst)["llm_backends"]}
        self.assertEqual(set(by_id), {"mine", "local", "cloud"})
        self.assertEqual(crypto.decrypt(dst, by_id["mine"]["api_key"]),
                         "my-key")

    def test_legacy_extra_body_travels_whether_stored_sealed_or_plain(self):
        """`llm_extra_body` can carry a credential and is stored encrypted,
        so it crosses master keys as a secret. Rows written before it was
        sealed are untagged plaintext — both shapes must arrive intact."""
        for stored, label in ((lambda c: crypto.encrypt(c, '{"a":1}'),
                               "sealed"),
                              (lambda c: '{"a":1}', "legacy plaintext")):
            with self.subTest(label):
                os.environ["OIKONOME_MASTER_KEY"] = \
                    Fernet.generate_key().decode()
                src = self._db()
                write_config(src, llm_url="https://llm.local/v1",
                             llm_model="gemma-3",
                             llm_extra_body=stored(src))
                blob = cb.export_bytes(src, "across-the-boxes")
                self.assertNotIn(b'{"a":1}', blob)

                os.environ["OIKONOME_MASTER_KEY"] = \
                    Fernet.generate_key().decode()
                dst = self._db()
                cb.import_bytes(dst, blob, "across-the-boxes")
                cfg = budget.load_config(dst)
                self.assertEqual(
                    crypto.decrypt(dst, cfg["llm_extra_body"]), '{"a":1}')

    def test_a_backend_url_gets_the_same_ssrf_guard_as_a_settings_save(self):
        """A bundle aims the worker at an endpoint exactly as a settings
        save does, so a crafted file must not be the door around the guard."""
        dst = self._db()
        with self.assertRaises(ValueError):
            cb.apply_payload(dst, {
                "oikonome_config": cb.FORMAT, "config": {}, "secrets": {},
                "llm_backends": [{"id": "evil", "name": "Evil", "model": "m",
                                  "url": "http://169.254.169.254/latest"}],
                "items": []})
        self.assertFalse(budget.load_config(dst).get("llm_backends"))

    def test_a_backend_cannot_smuggle_unknown_fields_or_a_reserved_id(self):
        dst = self._db()
        with self.assertRaises(ValueError):
            cb.apply_payload(dst, {
                "oikonome_config": cb.FORMAT, "config": {}, "secrets": {},
                "llm_backends": [{"id": "bundled", "url": "https://x.dev/v1",
                                  "model": "m"}],
                "items": []})
        cb.apply_payload(dst, {
            "oikonome_config": cb.FORMAT, "config": {}, "secrets": {},
            "llm_backends": [{"id": "ok", "url": "https://x.dev/v1",
                              "model": "m", "demo_login": "sneaky"}],
            "items": []})
        entry = budget.load_config(dst)["llm_backends"][0]
        self.assertNotIn("demo_login", entry)

    def test_a_bundle_without_backends_still_imports(self):
        """Bundles written before named backends existed carry no such key —
        opening one must not become an error or invent an empty list."""
        dst = self._db()
        out = cb.apply_payload(dst, {
            "oikonome_config": cb.FORMAT,
            "config": {"smtp_host": "mail.example"},
            "secrets": {}, "items": []})
        self.assertEqual(out["config"], ["smtp"])
        self.assertNotIn("llm_backends", budget.load_config(dst))


class BundleDoorStepUpTests(unittest.TestCase):
    """Both bundle doors decrypt/plant the tenant's whole
    credential set — a bare stolen session cookie must not reach them.
    Password step-up, like every other durable-credential door."""

    PW = "correct-horse-battery"

    @classmethod
    def setUpClass(cls):
        import uuid as _uuid

        from fastapi.testclient import TestClient
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"bexp-{_uuid.uuid4().hex[:8]}@example.dev",
            "password": cls.PW})
        assert r.status_code == 200, r.text

    def test_export_without_password_is_refused(self):
        r = self.client.post("/api/connections/export",
                             json={"passphrase": "hunter2hunter2"})
        self.assertEqual(r.status_code, 403)
        # the export wants a FRESH elevation, not just any
        self.assertEqual(r.json()["detail"],
                         {"error": "elevation_required", "fresh": True})

    def test_export_with_password_succeeds(self):
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        try:
            r = self.client.post("/api/connections/export",
                                 json={"passphrase": "hunter2hunter2",
                                       "password": self.PW})
            self.assertEqual(r.status_code, 200, r.text)
        finally:
            os.environ.pop("OIKONOME_MASTER_KEY", None)

    def test_import_without_password_is_refused(self):
        r = self.client.post(
            "/api/connections/import",
            data={"passphrase": "hunter2hunter2"},
            files={"file": ("x.oikx", b"garbage")})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "elevation_required")


class KdfEnvelopeBoundsTests(unittest.TestCase):
    """The .oikx envelope's scrypt work factors are attacker-authored (any
    passphrase seals a well-formed file), and scrypt's memory cost is
    ~128*n*r bytes — so n and r each within their individual bounds still
    multiply to a ~4 GiB allocation at the corners, spent inside derive()
    BEFORE the passphrase is checked. The products must be bounded too,
    and rejection must happen before any key derivation runs."""

    def _envelope(self, n, r, p):
        return json.dumps({
            "oikx": 1, "kdf": "scrypt", "n": n, "r": r, "p": p,
            "salt": base64.b64encode(b"s" * 16).decode(), "blob": "x",
        }).encode()

    def test_corner_params_rejected_before_any_key_derivation(self):
        # each dimension is individually in range; the products are not
        for n, r, p in ((2 ** 20, 32, 16),   # n*r ⇒ ~4.3 GiB
                        (2 ** 18, 32, 1),    # n*r just past the cap
                        (2 ** 17, 16, 16)):  # n*r ok, n*r*p is not
            with self.subTest(n=n, r=r, p=p):
                with mock.patch.object(
                        cb, "_derive",
                        side_effect=AssertionError("scrypt ran")):
                    with self.assertRaises(ValueError):
                        cb._open(self._envelope(n, r, p), "pw")

    def test_nondefault_params_within_bounds_still_open(self):
        """The cap must not break files sealed under a future retuning that
        stays within the ceiling."""
        salt = os.urandom(16)
        n, r, p = 2 ** 10, 2, 2
        token = Fernet(cb._derive("pw", salt, n, r, p)).encrypt(b"hello")
        env = json.dumps({
            "oikx": 1, "kdf": "scrypt", "n": n, "r": r, "p": p,
            "salt": base64.b64encode(salt).decode(),
            "blob": token.decode()}).encode()
        self.assertEqual(cb._open(env, "pw"), b"hello")

    def test_default_seal_still_roundtrips(self):
        self.assertEqual(cb._open(cb._seal(b"payload", "pw"), "pw"),
                         b"payload")


class MalformedItemsFieldTests(unittest.TestCase):
    """A bundle's `items` is attacker-shaped JSON. A non-list value would
    otherwise iterate anyway (a string per character, a dict per key) and
    crash with AttributeError — a 500 where every other malformed field
    gets a clean ValueError → 400."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_non_list_items_is_a_clean_reject_not_a_crash(self):
        for bad in ("abc", {"a": 1}, 5, 1.5, True):
            with self.subTest(items=bad):
                with self.assertRaises(ValueError):
                    cb.apply_payload(self.conn, {
                        "oikonome_config": cb.FORMAT, "items": bad})

    def test_null_and_empty_items_still_import(self):
        for ok in (None, []):
            with self.subTest(items=ok):
                out = cb.apply_payload(self.conn, {
                    "oikonome_config": cb.FORMAT, "items": ok})
                self.assertEqual(out["items"], 0)

    def test_non_dict_item_entries_are_skipped(self):
        out = cb.apply_payload(self.conn, {
            "oikonome_config": cb.FORMAT, "items": ["x", 3, None, []]})
        self.assertEqual(out["items"], 0)


if __name__ == "__main__":
    unittest.main()
