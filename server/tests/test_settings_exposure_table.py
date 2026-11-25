"""Every settings key has a decided audience, and no credential reaches a
viewer or an export.

The settings read goes to every member of a household, viewers included,
and the export ZIP carries the stored document out of the instance. A
credential can sit under a key not named like one (a relay's API token
stored as the SMTP username), so the rule is one table (web/settings_exposure.py); these tests fail when the
read grows a key the table does not classify, and when a viewer's view or
an export carries a secret-shaped value."""

import io
import json
import re
import unittest
import zipfile

from oikonome.sync import export
from oikonome.web import api, permissions, settings_exposure as se

from .util import make_db, write_config

# invented values, shaped like the real thing
TOKEN = "pm-0000aaaa-1111-bbbb-2222-cccc3333dddd"
URL_PW = "url-pass-9f8e7d"
QS_KEY = "qs-key-5a4b3c"
API_KEY = "sk-invented-123456"
SMTP_PW = "smtp-pass-invented"
LLM_HOST = "llm-endpoint.household.example"
LLM_MODEL = "house-model-7b"
BUNDLED_HOST = "bundled-ollama.local"
BUNDLED_MODEL = "bundled-model-3b"
SECRETS = (TOKEN, URL_PW, QS_KEY, API_KEY, SMTP_PW, "plaid-secret-x",
           "mx-key-x", "body-token-x")


def _rich_cfg() -> dict:
    """A stored settings document with every credential-bearing key set."""
    cfg = {k: None for k in api._SETTINGS_FIELDS}
    cfg.update({
        "food_monthly": 500, "other_monthly": 300,
        "smtp_host": "smtp.postmarkapp.example", "smtp_port": 587,
        "smtp_user": TOKEN, "smtp_password": SMTP_PW,
        "smtp_from": "budget@household.example",
        "llm_url": f"https://alice:{URL_PW}@llm.example/v1?api_key={QS_KEY}",
        "llm_api_key": API_KEY, "llm_extra_body": {"org": "body-token-x"},
        "llm_backends": [{
            "id": "b1", "name": "remote",
            "url": f"https://bob:{URL_PW}@llm2.example/v1?token={QS_KEY}",
            "model": "m", "api_key": API_KEY,
            "extra_body": {"org": "body-token-x"}}],
        "llm_roles": {"categorize": "b1"},
        "plaid_client_id": "client-id-public", "plaid_secret": "plaid-secret-x",
        "mx_client_id": "mx-client-public", "mx_api_key": "mx-key-x",
        "email_recipients": ["a@household.example"],
    })
    return cfg


def _view(cfg: dict) -> dict:
    v = api._settings_view(cfg)
    # what the settings door adds after the view is built
    v["email_recipient_status"] = []
    v["demo_read_only"] = True
    return v


# the household's setup, the owner's alone to read
OWNER_SETUP = frozenset({
    "smtp_host", "smtp_port", "smtp_from", "smtp_starttls",
    "email_recipients", "email_muted", "email_schedule", "email_send_hour_utc",
    "llm_url", "llm_model", "llm_vision_model", "llm_backends", "llm_roles",
    "llm_extra_body", "taxdocs_allow_remote_llm",
    "plaid_client_id", "plaid_env", "mx_client_id", "mx_env",
    "plaid_enrich_cap", "plaid_recurring",
    # the same endpoint and model read back through the resolver, and
    # whether the trained classifier runs ahead of it
    "llm_effective", "llm_bundled", "categorizer_active",
})

_SECRET_SHAPED = re.compile(r"//[^/@\s]*:[^*/@\s][^/@\s]*@")   # user:pw@


class EverySettingsKeyIsClassified(unittest.TestCase):
    def test_every_key_the_settings_read_returns_has_a_row(self):
        missing = sorted(set(_view(_rich_cfg())) - set(se.TABLE))
        self.assertEqual(missing, [], "classify these in "
                         "web/settings_exposure.py TABLE: %s" % missing)

    def test_every_settings_field_has_a_row(self):
        self.assertEqual(sorted(set(api._SETTINGS_FIELDS) - set(se.TABLE)),
                         [])

    def test_every_owner_only_write_key_has_a_row(self):
        # the save's owner-only list and the read's table describe the same
        # keys; a key one knows and the other does not is undecided
        self.assertEqual(sorted(permissions.OWNER_ONLY_SETTINGS
                                - set(se.TABLE) - {"smtp_no_verify",
                                                   "email_muted_add",
                                                   "email_muted_remove"}),
                         [])

    def test_the_rows_use_known_values(self):
        for k, e in se.TABLE.items():
            self.assertIn(e.read, ("viewer", "owner", "never"), k)
            self.assertIn(e.below, ("drop", "set"), k)

    def test_the_demo_door_keys_are_the_ones_the_settings_door_locks(self):
        self.assertEqual(se.DEMO_DOOR_KEYS, api._DEMO_DOOR_KEYS)


class NoCredentialReachesAViewer(unittest.TestCase):
    def _assert_clean(self, out: dict):
        blob = json.dumps(out, default=str)
        for s in SECRETS:
            self.assertNotIn(s, blob)
        self.assertIsNone(_SECRET_SHAPED.search(blob), blob)

    def test_a_viewer_gets_no_secret_shaped_value(self):
        out = se.view_for("viewer", _view(_rich_cfg()))
        self._assert_clean(out)
        self.assertNotIn("smtp_user", out)
        self.assertIs(out["smtp_user_set"], True)

    def test_a_member_gets_no_secret_shaped_value(self):
        self._assert_clean(se.view_for("member", _view(_rich_cfg())))

    def test_an_unknown_role_reads_as_a_viewer(self):
        out = se.view_for("auditor", _view(_rich_cfg()))
        self.assertNotIn("smtp_user", out)

    def test_the_owner_sees_the_relay_user_and_still_no_url_password(self):
        out = se.view_for("owner", _view(_rich_cfg()))
        self.assertEqual(out["smtp_user"], TOKEN)
        blob = json.dumps(out, default=str)
        for s in (URL_PW, QS_KEY, API_KEY, SMTP_PW):
            self.assertNotIn(s, blob)

    def test_an_unclassified_key_is_withheld_from_all_but_the_owner(self):
        v = {"some_future_key": "x"}
        self.assertEqual(se.view_for("member", v), {})
        self.assertEqual(se.view_for("owner", v), v)

    def test_the_household_data_still_reaches_a_viewer(self):
        out = se.view_for("viewer", _view(_rich_cfg()))
        self.assertEqual(out["food_monthly"], 500)
        self.assertIn("timezone", out)    # every screen dates by it

    def test_the_households_setup_is_the_owners_to_read(self):
        # where the mail goes and when, the AI endpoint, the bank-connection
        # keys and billed cross-checks: the owner's alone;
        # a member and a viewer get none of them
        view = _view(_rich_cfg())
        for role in ("member", "viewer"):
            with self.subTest(role=role):
                out = se.view_for(role, view)
                self.assertEqual(sorted(set(out) & OWNER_SETUP), [])
        owner = se.view_for("owner", view)
        self.assertEqual(owner["smtp_host"], "smtp.postmarkapp.example")
        self.assertEqual(owner["plaid_client_id"], "client-id-public")
        self.assertEqual(owner["email_recipients"], ["a@household.example"])

    def test_every_owner_only_write_but_timezone_is_owner_read(self):
        # the save's owner-only list and the read agree: a key only the
        # owner may change is one only the owner reads, but the clock
        readable = sorted(k for k in permissions.OWNER_ONLY_SETTINGS
                          if k in se.TABLE and se.TABLE[k].read == "viewer")
        self.assertEqual(readable, ["timezone"])


class NoCredentialRidesTheExport(unittest.TestCase):
    def test_export_view_drops_the_relay_user(self):
        out = se.export_view(_rich_cfg())
        self.assertNotIn("smtp_user", out)
        self.assertEqual(out["smtp_host"], "smtp.postmarkapp.example")
        blob = json.dumps(out, default=str)
        for s in SECRETS:
            self.assertNotIn(s, blob)

    def test_the_export_zip_settings_carry_no_credential(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, **{k: v for k, v in _rich_cfg().items()
                              if v is not None})
        data = export.build_zip(conn)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            text = z.read("tenant_settings.csv").decode()
        self.assertIn("smtp.postmarkapp.example", text)
        for s in SECRETS:
            self.assertNotIn(s, text)

    def test_a_closed_month_snapshot_carries_no_relay_user(self):
        out = export._scrub_snapshot({"smtp_user": TOKEN, "food_monthly": 1})
        self.assertNotIn("smtp_user", out)


class TheSettingsDoorAppliesTheTable(unittest.TestCase):
    """The settings read itself: a viewer's GET carries no relay token."""

    @classmethod
    def setUpClass(cls):
        import os
        import uuid

        from fastapi.testclient import TestClient

        from oikonome.db import tenancy

        from .util import _ensure_db, seed_accounts
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.owner = TestClient(appmod.app)
        cls.owner.post("/api/signup", data={
            "email": f"sxo-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, smtp_host="smtp.postmarkapp.example",
                         smtp_port=587, smtp_user=TOKEN,
                         smtp_from="budget@household.example",
                         llm_url=f"https://{LLM_HOST}/v1",
                         llm_model=LLM_MODEL)
        finally:
            conn.close()
        inv = cls.owner.post("/api/invites", json={
            "label": "spouse", "password": "correct-horse-battery"}).json()
        token = inv["url"].rsplit("token=", 1)[1]
        cls.viewer = TestClient(appmod.app)
        r = cls.viewer.post("/api/invite/claim", json={
            "token": token,
            "email": f"sxv-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        assert r.status_code == 200, r.text
        inv = cls.owner.post("/api/invites", json={
            "label": "partner", "role": "member",
            "password": "correct-horse-battery"}).json()
        cls.member = TestClient(appmod.app)
        r = cls.member.post("/api/invite/claim", json={
            "token": inv["url"].rsplit("token=", 1)[1],
            "email": f"sxm-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        assert r.status_code == 200, r.text

    def _read(self, client) -> str:
        # the operator's bundled endpoint too, as a self-hosted install has
        from unittest import mock
        with mock.patch.dict("os.environ", {
                "OIKONOME_LLM_URL": f"http://{BUNDLED_HOST}:11434",
                "OIKONOME_LLM_MODEL": BUNDLED_MODEL}):
            r = client.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        return r.text

    def test_no_non_owner_reads_the_ai_endpoint_or_its_model(self):
        # the endpoint and model are the owner's to read; the views of them
        # the AI card works from (llm_effective, llm_bundled) are the same
        # setup and must not hand them to anyone else
        for who, client in (("member", self.member),
                            ("viewer", self.viewer)):
            with self.subTest(role=who):
                self.assertEqual(client.get("/api/me").json()["role"], who)
                text = self._read(client)
                for value in (LLM_HOST, LLM_MODEL, BUNDLED_HOST,
                              BUNDLED_MODEL):
                    self.assertNotIn(value, text)
                body = json.loads(text)
                for key in OWNER_SETUP:
                    self.assertNotIn(key, body)

    def test_the_owner_reads_which_endpoint_answers(self):
        body = json.loads(self._read(self.owner))
        self.assertIn(LLM_HOST, json.dumps(body["llm_effective"]))
        self.assertEqual(body["llm_bundled"]["model"], BUNDLED_MODEL)
        self.assertIn("categorizer_active", body)

    def test_a_viewer_reads_that_a_relay_user_is_set_not_what_it_is(self):
        self.assertEqual(self.viewer.get("/api/me").json()["role"], "viewer")
        r = self.viewer.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(TOKEN, r.text)
        self.assertIs(r.json().get("smtp_user_set"), True)

    def test_the_owner_still_reads_the_relay_user(self):
        r = self.owner.get("/api/settings")
        self.assertEqual(r.json().get("smtp_user"), TOKEN)


if __name__ == "__main__":
    unittest.main()
