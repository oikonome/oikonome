"""A household member edits the money; the account stays the owner's.

Two people running one set of books need more than view-only and less than
owner, so 'member' sits between them: every edit a viewer is refused, none
of the acts that end the account, take the data out, change who is in the
house, or touch a bank connection.

These tests are the contract from both ends: what a member can do (or the
role is pointless) and what a member cannot (or it is not a role at all).
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db, seed_accounts, write_config

PW = "correct-horse-battery"
MEMBER_PW = "family-member-pass"


class MemberRoleTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app
        cls.owner = TestClient(app)
        cls.owner.post("/api/signup", data={
            "email": f"own-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        # repointing mail, the AI endpoint or the daily report is a step-up act
        cls.owner.post("/api/auth/elevate",
                       json={"password": "correct-horse-battery"})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        cls.member, cls.member_email = cls._claim(cls, "member")

    def _claim(self, role, label="partner"):
        """Mint an invite for `role` and claim it; returns (client, email)."""
        r = self.owner.post("/api/invites",
                            json={"label": label, "role": role,
                                  "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        client = TestClient(self.app)
        email = f"{role}-{uuid.uuid4().hex[:8]}@example.dev"
        got = client.post("/api/invite/claim", json={
            "token": token, "email": email, "password": MEMBER_PW})
        assert got.status_code == 200, got.text
        return client, email

    def _config(self) -> dict:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    # ---- the invite carries the role -----------------------------------

    def test_the_invite_says_which_role_it_creates(self):
        r = self.owner.post("/api/invites",
                            json={"label": "reader", "role": "viewer",
                                  "password": PW})
        self.assertEqual(r.status_code, 200)
        token = r.json()["url"].rsplit("token=", 1)[1]
        peek = self.owner.get(f"/api/invite/peek?token={token}")
        self.assertEqual(peek.json()["role"], "viewer")

    def test_an_invite_cannot_mint_an_owner(self):
        """There is one owner. Handing the account to somebody else is a
        different act from adding a person, and a dropdown must not be
        able to perform it by accident."""
        r = self.owner.post("/api/invites",
                            json={"label": "sneaky", "role": "owner",
                                  "password": PW})
        self.assertEqual(r.status_code, 400)

    def test_the_claimed_member_reads_as_one(self):
        me = self.member.get("/api/me").json()
        self.assertEqual((me["role"], me["email"]),
                         ("member", self.member_email))

    # ---- what a member CAN do ------------------------------------------

    def test_a_member_edits_the_household_money(self):
        r = self.member.post("/api/settings", json={"food_monthly": 777})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._config()["food_monthly"], 777)
        # and the working doors a viewer is refused outright
        self.assertEqual(self.member.post("/api/bills/detect").status_code,
                         200)
        self.assertEqual(
            self.member.post("/api/rules/set",
                             json={"merchant": "Corner Store",
                                   "category": "FOOD_AND_DRINK"}).status_code,
            200)

    def test_a_viewer_is_still_refused_the_same_edits(self):
        viewer, _ = self._claim("viewer", label="reader-2")
        self.assertEqual(
            viewer.post("/api/settings",
                        json={"food_monthly": 1}).status_code, 403)
        self.assertEqual(viewer.post("/api/bills/detect").status_code, 403)

    def test_a_viewer_may_ask_the_read_only_assistant(self):
        """Everybody in a household reads everything, and the assistant is
        a read: it routes tool calls that only SELECT and answers in prose.
        It is a POST because the question does not fit a query string, and
        the write gate reads the METHOD, so the assistant must be exempt;
        otherwise a viewer is refused for asking a question, with no way
        round it from either client. The refusal must not be a role one;
        an instance with no LLM configured still answers 503, and that is
        the assistant declining, not the household.
        """
        viewer, _ = self._claim("viewer", label="reader-3")
        r = viewer.post("/api/assistant", json={"question": "how am I doing?"})
        self.assertNotEqual(403, r.status_code, r.text)
        self.assertNotIn("view-only", r.text)
        # saving an answer as a report is the same read in a file
        r = viewer.post("/api/assistant/report", json={
            "question": "how am I doing?",
            "blocks": [{"kind": "text", "text": "fine"}]})
        self.assertNotEqual(403, r.status_code, r.text)
        self.assertNotIn("view-only", r.text)
        # and the role still means something: a genuine write is refused
        self.assertEqual(
            viewer.post("/api/rules/set",
                        json={"merchant": "Corner Store",
                              "category": "FOOD_AND_DRINK"}).status_code, 403)

    # ---- what a member CANNOT do ---------------------------------------

    def test_a_member_cannot_end_the_account(self):
        r = self.member.post("/api/account/delete",
                             data={"password": MEMBER_PW, "confirm": "DELETE"})
        self.assertEqual(r.status_code, 403)

    def test_a_member_cannot_take_the_data_out(self):
        r = self.member.post("/api/export/token",
                             json={"kind": "zip", "password": MEMBER_PW})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(
            self.member.post("/api/connections/export",
                             json={"password": MEMBER_PW}).status_code, 403)
        # the download door reads its own authorization too, so a ticket
        # minted before a demotion is not a way back in
        self.assertEqual(self.member.get("/export?t=whatever").status_code,
                         403)

    def test_a_member_cannot_restore_an_export_over_the_household(self):
        """A ZIP through the import door is not an import — it merges
        another whole ledger. Same role as the export it mirrors."""
        r = self.member.post(
            "/api/import",
            files={"file": ("backup.zip", b"PK\x03\x04not-a-real-zip",
                            "application/zip")},
            data={"account_id": "", "amount_sign": "bank"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("owner", r.json()["detail"])

    def test_a_member_cannot_change_who_is_in_the_household(self):
        self.assertEqual(
            self.member.post("/api/invites",
                             json={"label": "x", "password": MEMBER_PW}
                             ).status_code, 403)
        self.assertEqual(
            self.member.request("DELETE", f"/api/users/{uuid.uuid4()}",
                                json={"password": MEMBER_PW}).status_code, 403)
        self.assertEqual(
            self.member.post(f"/api/users/{uuid.uuid4()}/role",
                             json={"role": "viewer", "password": MEMBER_PW}
                             ).status_code, 403)
        # …nor mint a credential that would let something else in
        self.assertEqual(
            self.member.post("/api/tokens",
                             json={"name": "collector", "password": MEMBER_PW}
                             ).status_code, 403)

    def test_a_member_cannot_touch_the_bank_connections(self):
        self.assertEqual(
            self.member.post("/api/accounts/plaid/keys",
                             json={"client_id": "x", "secret": "y"}
                             ).status_code, 403)
        self.assertEqual(
            self.member.post(f"/api/connections/{uuid.uuid4()}/disconnect"
                             ).status_code, 403)
        self.assertEqual(
            self.member.post("/api/accounts/some-account/remove",
                             json={"mode": "disconnect"}).status_code, 403)

    def test_a_members_settings_save_leaves_the_owners_fields_alone(self):
        """One endpoint carries the whole settings document, and the app
        posts all of it on every save — so the owner-only fields are
        dropped from a member's body rather than refused. A member editing
        a budget must not be able to re-point the household's mail."""
        self.owner.post("/api/settings",
                        json={"smtp_host": "mail.example.dev",
                              "smtp_port": 587,
                              "smtp_from": "money@example.dev"})
        before = self._config()
        self.assertEqual(before.get("smtp_host"), "mail.example.dev")
        r = self.member.post("/api/settings",
                             json={"food_monthly": 812,
                                   "smtp_host": "attacker.example",
                                   "email_recipients": "elsewhere@evil.test"})
        self.assertEqual(r.status_code, 200, r.text)
        after = self._config()
        self.assertEqual(after["food_monthly"], 812)       # their edit landed
        self.assertEqual(after.get("smtp_host"), "mail.example.dev")
        self.assertEqual(after.get("email_recipients"),
                         before.get("email_recipients"))

    def test_only_the_owner_changes_how_far_back_the_history_reaches(self):
        """The window sets what the next bank link asks the aggregator for,
        and a Plaid link's backfill cannot be widened after it is made, so
        a member may not narrow it for the owner's next link. A changed
        value is refused outright (the client must not show it saved);
        the stored value echoed back by a whole-document save is not a
        change, and the member's other edits in that save still land."""
        self.assertEqual(self.owner.post(
            "/api/settings", json={"history_months": 12}).status_code, 200)
        for v in (1, None):
            r = self.member.post("/api/settings",
                                 json={"food_monthly": 61,
                                       "history_months": v})
            self.assertEqual(r.status_code, 403, (v, r.text))
            self.assertEqual(self._config()["history_months"], 12)
            self.assertNotEqual(self._config()["food_monthly"], 61)
        r = self.member.post("/api/settings",
                             json={"food_monthly": 62, "history_months": 12})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._config()["food_monthly"], 62)
        r = self.member.post("/api/settings", json={"history_months": 1})
        self.assertEqual(r.json(), {"detail": (
            "only the household's owner can change how far back its "
            "history reaches")})
        self.owner.post("/api/settings", json={"history_months": None})

    def test_only_the_owner_changes_how_the_bills_are_found(self):
        """Choosing manual setup, or turning detection off, stops the
        nightly finder and the bank's stream offering new bills and income
        to the whole household — the owner's call, refused (not dropped)
        for a member the same way as the history window. Echoing the stored
        choices back in a whole-document save still goes through."""
        self.assertEqual(self.owner.post(
            "/api/settings", json={"setup_mode": "auto",
                                   "bill_detection_auto": True}
        ).status_code, 200)
        refusals = {
            "setup_mode": ("manual", "only the household's owner can "
                           "change how the household's bills are set up"),
            "bill_detection_auto": (False, "only the household's owner can "
                                    "change whether new bills and income "
                                    "are found automatically"),
        }
        for key, (v, why) in refusals.items():
            r = self.member.post("/api/settings",
                                 json={"food_monthly": 71, key: v})
            self.assertEqual(r.status_code, 403, (key, r.text))
            self.assertEqual(r.json(), {"detail": why})
            self.assertNotEqual(self._config()["food_monthly"], 71)
        cfg = self._config()
        self.assertNotEqual(cfg.get("setup_mode"), "manual")
        self.assertIsNot(cfg.get("bill_detection_auto"), False)
        # the stored choices echoed back (absent setup_mode reads as auto)
        r = self.member.post("/api/settings",
                             json={"food_monthly": 72, "setup_mode": "auto",
                                   "bill_detection_auto": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._config()["food_monthly"], 72)
        r = self.member.post("/api/settings",
                             json={"food_monthly": 73, "setup_mode": None})
        self.assertEqual(r.status_code, 200, r.text)
        # the owner still decides
        r = self.owner.post("/api/settings",
                            json={"setup_mode": "manual"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(self._config()["bill_detection_auto"], False)
        # ...and a member echoing manual back does not turn detection off
        # again once the owner turned it back on
        self.owner.post("/api/settings", json={"bill_detection_auto": True})
        r = self.member.post("/api/settings",
                             json={"food_monthly": 74, "setup_mode": "manual",
                                   "bill_detection_auto": True})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.member.post("/api/settings",
                             json={"food_monthly": 75, "setup_mode": "manual"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(self._config()["bill_detection_auto"], True)
        self.owner.post("/api/settings",
                        json={"setup_mode": "auto",
                              "bill_detection_auto": True})

    def test_the_refused_settings_are_the_clients_owner_only_keys(self):
        """The clients render these owner-only on the strength of this set;
        a key leaving it would turn a member's 403 into a silent change."""
        from oikonome.web import permissions
        self.assertEqual(set(permissions.OWNER_REFUSED_SETTINGS),
                         {"history_months", "setup_mode",
                          "bill_detection_auto"})

    def test_a_member_cannot_send_sensitive_documents_off_box(self):
        """The AI card — which backend serves each role, the vision model,
        and the consent that lets a W-2's SSN or a check's account number
        leave the box — is owner-only in both clients, so a member's raw
        settings POST must not flip it either."""
        r = self.member.post("/api/settings",
                             json={"food_monthly": 5,
                                   "taxdocs_allow_remote_llm": "1",
                                   "llm_roles": {"vision": "evil"},
                                   "llm_vision_model": "leak",
                                   "llm_model": "sneaky",
                                   "llm_extra_body": {"x": 1}})
        self.assertEqual(r.status_code, 200, r.text)
        after = self._config()
        self.assertNotEqual(after.get("taxdocs_allow_remote_llm"), "1")
        self.assertNotEqual(after.get("llm_vision_model"), "leak")
        # llm_model and llm_extra_body are the same owner-only group
        self.assertNotEqual(after.get("llm_model"), "sneaky")
        self.assertNotEqual(after.get("llm_extra_body"), {"x": 1})

    def test_owner_only_settings_are_all_dropped_from_a_members_save(self):
        """One place, every owner-only key: a member's raw settings POST
        drops each one rather than relying on client-side owner-gating
        alone. Pins the whole set so a field the clients render owner-only
        but the server forgets to gate fails here instead of in the wild."""
        from oikonome.web import permissions
        # a value distinguishable from any plausible stored default
        probe = {k: (["evil@x"] if k == "email_muted" else "ZZZ-member")
                 for k in permissions.OWNER_ONLY_SETTINGS}
        probe["food_monthly"] = 999                      # a real member edit
        r = self.member.post("/api/settings", json=probe)
        self.assertEqual(r.status_code, 200, r.text)
        after = self._config()
        self.assertEqual(after["food_monthly"], 999)     # the edit landed
        leaked = [k for k in permissions.OWNER_ONLY_SETTINGS
                  if after.get(k) == "ZZZ-member"
                  or (k == "email_muted" and after.get(k) == ["evil@x"])]
        self.assertEqual(leaked, [],
                         f"member POST changed owner-only keys: {leaked}")

    def test_a_member_cannot_edit_the_mute_list_one_address_at_a_time(self):
        """The one-address delta keys edit the same stored mute and
        recipient lists as the whole-list keys, so a member's save drops
        them too — otherwise a member could silence the owner, or un-mute
        someone who opted out, by spelling the key differently."""
        self.owner.post("/api/settings", json={"email_muted": ["quiet@x"]})
        r = self.member.post("/api/settings",
                             json={"email_muted_add": ["owner@x"],
                                   "email_muted_remove": ["quiet@x"],
                                   "email_recipients_added": ["new@x"],
                                   "email_recipients_removed": ["quiet@x"],
                                   "food_monthly": 321})
        self.assertEqual(r.status_code, 200, r.text)
        after = self._config()
        self.assertEqual(after["food_monthly"], 321)
        self.assertEqual(after.get("email_muted"), ["quiet@x"])

    # ---- the owner moves people between roles --------------------------

    def test_the_owner_moves_a_member_to_view_only_and_back(self):
        client, email = self._claim("member", label="demote-me")
        uid = next(u["id"] for u in
                   self.owner.get("/api/users").json()["users"]
                   if u["email"] == email)
        r = self.owner.post(f"/api/users/{uid}/role",
                            json={"role": "viewer", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(client.get("/api/me").json()["role"], "viewer")
        self.assertEqual(
            client.post("/api/settings", json={"food_monthly": 3}).status_code,
            403)
        r = self.owner.post(f"/api/users/{uid}/role",
                            json={"role": "member", "password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(client.get("/api/me").json()["role"], "member")

    def test_changing_a_role_re_asks_for_the_password(self):
        """Handing somebody edit rights is a durable grant — a stolen
        cookie must not be enough, the same bar minting an invite meets."""
        client, email = self._claim("viewer", label="stepup-target")
        uid = next(u["id"] for u in
                   self.owner.get("/api/users").json()["users"]
                   if u["email"] == email)
        r = self.owner.post(f"/api/users/{uid}/role",
                            json={"role": "member", "password": "wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(client.get("/api/me").json()["role"], "viewer")

    def test_the_roster_names_the_roles_the_owner_may_pick(self):
        got = self.owner.get("/api/users").json()
        self.assertEqual(got["assignable_roles"], ["member", "viewer"])

    def test_an_owner_cannot_demote_themselves_or_promote_anyone(self):
        users = self.owner.get("/api/users").json()["users"]
        mine = next(u["id"] for u in users if u["me"])
        self.assertEqual(
            self.owner.post(f"/api/users/{mine}/role",
                            json={"role": "viewer",
                                  "password": PW}).status_code, 400)
        other = next(u["id"] for u in users if not u["me"])
        self.assertEqual(
            self.owner.post(f"/api/users/{other}/role",
                            json={"role": "owner",
                                  "password": PW}).status_code, 400)
        self.assertEqual(self.owner.get("/api/me").json()["role"], "owner")


if __name__ == "__main__":
    unittest.main()
