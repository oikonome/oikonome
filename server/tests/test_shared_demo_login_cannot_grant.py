"""The shared demonstration login cannot make a grant that outlives it.

That login is an owner whose password is handed out and whose second
factor is waived, so the step-up in front of every durable grant proves
nothing for it. A member account claimed from an invite it minted, a
script token, a support window, a webhook, a new mail recipient — each
would survive the rotation of the shared password, which is the operator's
only remedy when it leaks. So every door that mints a principal or opens an
outbound channel refuses it, a link it minted cannot be claimed, and its
view of the household's people — the roster, the mail list, the activity
filter, the continuity page and packet — is only itself, and a save of
what it was shown leaves the rest in place. Nor can it link a bank, an
exchange or an aggregator's keys, or subscribe a browser or register a
phone for the household's alerts. Ordinary owners are unaffected."""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class SharedDemoLoginGrants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _login(self, waived: bool):
        c = TestClient(self.app)
        email = f"grant-{uuid.uuid4().hex[:10]}@example.dev"
        data = {"email": email, "password": PW}
        if os.environ.get("OIKONOME_HOSTED"):
            from oikonome.auth import signup_invites
            admin = tenancy.admin_connect()
            try:
                data["invite"] = signup_invites.mint(admin, email)
            finally:
                admin.close()
        self.assertEqual(c.post("/api/signup", data=data).status_code, 200)
        admin = tenancy.admin_connect()
        try:
            if waived:
                admin.execute("UPDATE users SET second_factor_waived=TRUE "
                              "WHERE email=%s", (email,))
            row = admin.execute("SELECT id, tenant_id FROM users "
                                "WHERE email=%s", (email,)).fetchone()
        finally:
            admin.close()
        return c, email, row

    def _count(self, sql, arg):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(sql, (arg,)).fetchone()["n"]
        finally:
            admin.close()

    def test_me_tells_the_clients_which_login_is_shared(self):
        # the clients hide the refused controls on this flag, so it must
        # be true exactly for the login the refusing doors refuse
        shared, _, _ = self._login(waived=True)
        r = shared.get("/api/me")
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.json()["shared_login"], True)
        ordinary, _, _ = self._login(waived=False)
        r = ordinary.get("/api/me")
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.json()["shared_login"], False)

    def test_grant_doors_refuse_the_shared_login(self):
        c, _, row = self._login(waived=True)
        stranger = str(uuid.uuid4())
        doors = [
            ("post", "/api/invites", {"json": {"password": PW,
                                               "role": "member"}}),
            ("post", "/api/tokens", {"json": {"password": PW,
                                              "name": "script"}}),
            ("post", "/api/support-access", {"json": {"password": PW}}),
            ("post", "/api/webhooks", {"json": {
                "password": PW, "url": "https://hooks.example.dev/x"}}),
            ("post", f"/api/users/{stranger}/role", {"json": {
                "password": PW, "role": "viewer"}}),
            ("delete", f"/api/users/{stranger}", {"json": {"password": PW}}),
            ("post", "/api/settings", {"json": {
                "password": PW,
                "email_recipients": ["someone-new@example.dev"],
                "email_recipients_added": ["someone-new@example.dev"]}}),
            # a hook it was never shown: neither deleted nor pinged by it
            ("post", f"/api/webhooks/{stranger}/delete", {"json": {}}),
            ("post", f"/api/webhooks/{stranger}/test", {"json": {}}),
        ]
        for method, path, kw in doors:
            r = getattr(c, method)(path, **kw) if method == "post" else \
                c.request(method.upper(), path, **kw)
            self.assertEqual(r.status_code, 403, f"{path}: {r.text}")
            self.assertIn("shared demonstration login", r.text, path)
        tid = row["tenant_id"]
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM invites WHERE tenant_id=%s", tid), 0)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM api_tokens WHERE tenant_id=%s", tid),
            0)

    def _zip(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("settings.json",
                       '{"llm_url": "https://llm.example.dev/v1"}')
        return buf.getvalue()

    def test_bundle_and_restore_doors_refuse_the_shared_login(self):
        """The credentials bundle (both ways) and every whole-ledger
        restore land or hand out the household's AI endpoints, mail relay
        and bank links wholesale — none of them is open to the shared
        login, whatever its password step-up says."""
        c, _, row = self._login(waived=True)
        z = self._zip()
        staged = c.post("/api/import/bulk/analyze",
                        files=[("files", ("backup.zip", z,
                                          "application/zip"))])
        self.assertEqual(staged.status_code, 200, staged.text)
        token = staged.json()["token"]
        doors = [
            ("/api/connections/export", {"json": {
                "password": PW, "passphrase": "a-long-passphrase"}}),
            ("/api/connections/import", {
                "data": {"password": PW, "passphrase": "a-long-passphrase"},
                "files": {"file": ("c.oikx", b"not-a-bundle",
                                   "application/octet-stream")}}),
            ("/api/import", {
                "data": {"password": PW},
                "files": {"file": ("backup.zip", z, "application/zip")}}),
            ("/import", {
                "data": {"password": PW},
                "files": {"file": ("backup.zip", z, "application/zip")}}),
            ("/api/import/bulk/run", {"json": {
                "password": PW, "token": token,
                "files": [{"index": 0, "action": "import"}]}}),
        ]
        for path, kw in doors:
            r = c.post(path, **kw)
            self.assertEqual(r.status_code, 403, f"{path}: {r.text}")
            self.assertIn("shared demonstration login", r.text, path)
        from oikonome.engine import budget
        conn = tenancy.tenant_connect(row["tenant_id"])
        try:
            self.assertNotIn("llm.example.dev",
                             str(budget.load_config(conn)))
        finally:
            conn.close()

    def test_an_ordinary_owner_still_exports_the_bundle(self):
        c, _, _ = self._login(waived=False)
        r = c.post("/api/connections/export", json={
            "password": PW, "passphrase": "a-long-passphrase"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_an_invite_the_shared_login_minted_cannot_be_claimed(self):
        _, _, row = self._login(waived=True)
        from oikonome.auth import invites
        admin = tenancy.admin_connect()
        try:
            inv = invites.create(admin, row["tenant_id"], row["id"],
                                 role="member",
                                 label="joiner@example.dev")
        finally:
            admin.close()
        r = TestClient(self.app).post("/api/invite/claim", json={
            "token": inv["token"], "email": "joiner@example.dev",
            "password": "a-long-joiner-passphrase"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM users WHERE tenant_id=%s",
            row["tenant_id"]), 1)

    def test_the_shared_login_sees_only_itself_in_the_roster(self):
        c, email, row = self._login(waived=True)
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member')",
                (row["tenant_id"], f"m-{uuid.uuid4().hex[:6]}@example.dev"))
        finally:
            admin.close()
        users = c.get("/api/users").json()["users"]
        self.assertEqual([u["email"] for u in users], [email])

    def test_the_shared_login_reads_no_other_members_address(self):
        """The mail recipients in /api/settings, the actors in
        /api/activity and the invite labels in /api/invites are all other
        members' addresses; the shared login is shown none of them."""
        c, email, row = self._login(waived=True)
        other = f"m-{uuid.uuid4().hex[:6]}@example.dev"
        invitee = f"i-{uuid.uuid4().hex[:6]}@example.dev"
        tid = row["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, 'x', 'member', now())",
                (tid, other))
            from oikonome.auth import invites
            invites.create(admin, tid, row["id"], role="member",
                           label=invitee)
        finally:
            admin.close()
        from oikonome.engine import activity
        conn = tenancy.tenant_connect(tid)
        try:
            activity.record(conn, other, "note", "add", label="a note")
            activity.record(conn, email, "note", "add", label="mine")
        finally:
            conn.close()
        bodies = {path: c.get(path) for path in
                  ("/api/settings", "/api/activity", "/api/invites")}
        for path, r in bodies.items():
            self.assertEqual(r.status_code, 200, f"{path}: {r.text}")
            self.assertNotIn(other, r.text, path)
            self.assertNotIn(invitee, r.text, path)
        acts = bodies["/api/activity"].json()
        self.assertIn(email, acts["actors"])       # its own edits still name it
        r = c.get("/api/activity", params={"who": other})
        self.assertEqual(r.json()["rows"], [])
        # an ordinary owner of the same household still sees everyone
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET second_factor_waived=FALSE "
                          "WHERE id=%s", (row["id"],))
        finally:
            admin.close()
        c.post("/api/logout")
        c.post("/api/login", data={"email": email, "password": PW})
        self.assertIn(other, c.get("/api/activity").text)
        self.assertIn(other, c.get("/api/settings").text)

    def test_the_activity_feed_names_no_address_in_its_sentences(self):
        """A row's label and sentence can carry an address — mailing the
        continuity packet records who it went to. The shared login reads
        the row, but the only address left in it is its own."""
        import re as _re

        c, email, row = self._login(waived=True)
        owner = f"o-{uuid.uuid4().hex[:6]}@example.dev"
        member = f"m-{uuid.uuid4().hex[:6]}@example.dev"
        from oikonome.engine import activity
        conn = tenancy.tenant_connect(row["tenant_id"])
        try:
            activity.record(conn, owner, "settings", "continuity_emailed",
                            label=member)
            activity.record(conn, owner, "note", "add", label="Rent",
                            detail={"after": f"ask {member} about it"})
            activity.record(conn, email, "settings", "continuity_emailed",
                            label=email)
        finally:
            conn.close()
        r = c.get("/api/activity")
        self.assertEqual(r.status_code, 200, r.text)
        found = set(_re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", r.text))
        self.assertEqual(found, {email}, r.text)
        rows = r.json()["rows"]
        self.assertTrue(any(x["summary"] == "emailed the continuity packet "
                            "to a household member" for x in rows), rows)
        self.assertTrue(any(x["summary"] == "emailed the continuity packet "
                            f"to {email}" for x in rows), rows)

    def _household_with_another_member(self):
        """The shared login plus one other verified member who is on the
        mail list and has muted it, an activity row by each, and an owner's
        continuity note."""
        c, email, row = self._login(waived=True)
        other = f"m-{uuid.uuid4().hex[:6]}@example.dev"
        tid = row["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, 'x', 'member', now())",
                (tid, other))
        finally:
            admin.close()
        from oikonome.engine import activity, budget, continuity
        conn = tenancy.tenant_connect(tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["email_recipients"] = [email, other]
                cfg["email_muted"] = [other]
                cfg[continuity.KEY_NOTE] = "the will is with the lawyer"
                cfg[continuity.KEY_FOR] = "the executor"
            activity.record(conn, other, "note", "add", label="theirs")
            activity.record(conn, email, "note", "add", label="mine")
        finally:
            conn.close()
        return c, email, other, tid

    def _stored(self, tid):
        from oikonome.engine import budget
        conn = tenancy.tenant_connect(tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def test_continuity_shows_the_shared_login_only_itself(self):
        c, email, other, _ = self._household_with_another_member()
        r = c.get("/api/continuity")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(other, r.text)
        body = r.json()
        self.assertEqual(body["members"], [email])
        self.assertEqual((body["note"], body["prepared_for"]), ("", ""))
        # it is shown an empty note, so it cannot save over the real one
        r = c.put("/api/continuity", json={"note": ""})
        self.assertEqual(r.status_code, 403, r.text)

    def test_the_continuity_packet_prints_only_the_shared_login(self):
        from unittest import mock
        from oikonome.engine import continuity
        c, email, other, _ = self._household_with_another_member()
        t = c.post("/api/export/token", json={"kind": "continuity",
                                              "password": PW})
        self.assertEqual(t.status_code, 200, t.text)
        seen = {}

        def render(packet):
            seen.update(packet)
            return b"%PDF-"
        with mock.patch.object(continuity, "render_pdf", side_effect=render):
            r = c.get(t.json()["url"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([p["email"] for p in seen["people"]], [email])
        self.assertEqual((seen["note"], seen["prepared_for"]), ("", ""))

    def test_the_full_export_is_refused_to_the_shared_login(self):
        c, _, _, _ = self._household_with_another_member()
        for kind in ("zip", "dump"):
            r = c.post("/api/export/token", json={"kind": kind,
                                                  "password": PW})
            self.assertEqual(r.status_code, 403, f"{kind}: {r.text}")

    def test_a_settings_save_answers_like_a_read(self):
        c, email, other, _ = self._household_with_another_member()
        r = c.post("/api/settings", json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(other, r.text)
        self.assertEqual(r.json()["email_recipients"], [email])

    def test_a_settings_save_keeps_the_entries_it_was_not_shown(self):
        """Both clients post the recipient and mute lists whole, and the
        shared login is shown only its own row of each: muting itself and
        dropping itself off the list must leave the other member's entries
        where they were."""
        c, email, other, tid = self._household_with_another_member()
        r = c.post("/api/settings", json={"email_recipients": [email],
                                          "email_muted": [email]})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = self._stored(tid)
        self.assertIn(other, cfg["email_recipients"])
        self.assertEqual(sorted(cfg["email_muted"]), sorted([email, other]))
        r = c.post("/api/settings", json={"email_recipients": [],
                                          "email_muted": []})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = self._stored(tid)
        self.assertEqual(cfg["email_recipients"], [other])
        self.assertEqual(cfg["email_muted"], [other])

    def test_the_activity_filter_offers_only_the_shared_login(self):
        from oikonome.web import demoguard
        c, email, _, _ = self._household_with_another_member()
        body = c.get("/api/activity").json()
        self.assertEqual(body["actors"], [email])
        r = c.get("/api/activity", params={"who": demoguard.SOMEONE})
        self.assertEqual(r.json()["rows"], [])

    def test_resending_an_invite_does_not_confirm_another_member(self):
        c, _, other, _ = self._household_with_another_member()
        on_list = c.post("/api/settings/recipients/resend",
                         json={"email": other})
        nobody = c.post("/api/settings/recipients/resend",
                        json={"email": "nobody@example.dev"})
        self.assertEqual(on_list.status_code, 400, on_list.text)
        self.assertEqual(on_list.json(), nobody.json())

    # every door that puts a live credential into the household: a bank or
    # exchange link, or an aggregator's keys. The bodies are well-formed,
    # so a door that let the shared login through would reach its work.
    CREDENTIAL_DOORS = [
        ("POST", "/accounts/plaid/link", {
            "data": {}, "headers": {"Accept": "application/json"}}),
        ("POST", "/accounts/simplefin", {
            "data": {"simplefin_token": "not-a-real-token"}}),
        ("POST", "/api/accounts/simplefin", {
            "json": {"token": "not-a-real-token"}}),
        ("POST", "/api/accounts/coinbase/link", {"json": {
            "label": "visitor", "key_name": "organizations/x/apiKeys/y",
            "private_key": "not-a-real-key"}}),
        ("POST", "/api/accounts/mx/connect", {}),
        ("POST", "/api/accounts/mx/keys", {"json": {
            "client_id": "visitor", "api_key": "not-a-real-key"}}),
        ("POST", "/api/accounts/plaid/keys", {"json": {
            "client_id": "visitor", "secret": "not-a-real-secret"}}),
        ("POST", "/api/accounts/plaid/validate", {"json": {
            "client_id": "visitor", "secret": "not-a-real-secret"}}),
        ("DELETE", "/api/accounts/plaid/keys", {}),
        ("DELETE", "/api/accounts/mx/keys", {}),
        # billed per row to the Plaid account behind the instance
        ("POST", "/api/import/enrich", {"json": {"limit": 5}}),
    ]

    def test_credential_doors_refuse_the_shared_login(self):
        """A bank, exchange or aggregator key linked with the handed-out
        password is read by every later holder of it, keeps syncing (and
        billing the operator) every night, and survives the password's
        rotation — so none of these doors is open to the shared login."""
        c, _, row = self._login(waived=True)
        for method, path, kw in self.CREDENTIAL_DOORS:
            r = c.request(method, path, **kw)
            self.assertEqual(r.status_code, 403, f"{path}: {r.text}")
            self.assertIn("shared demonstration login", r.text, path)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM items WHERE tenant_id=%s",
            row["tenant_id"]), 0)

    def _household_with_a_bank(self, waived: bool):
        c, email, row = self._login(waived=waived)
        conn = tenancy.tenant_connect(row["tenant_id"])
        try:
            conn.execute("INSERT INTO items (id, aggregator, "
                         "institution_name, access_token, status) VALUES "
                         "('bank1','manual','Hand Bank','x','ok'), "
                         "('mx1','mx','MX Bank','x','archived')")
            conn.execute("INSERT INTO accounts (id, item_id, name, type) "
                         "VALUES ('acct1','bank1','Checking','depository')")
        finally:
            conn.close()
        return c, email, row

    def _item_status(self, tid, item_id):
        conn = tenancy.tenant_connect(tid)
        try:
            return conn.execute("SELECT status FROM items WHERE id=%s",
                                (item_id,)).fetchone()["status"]
        finally:
            conn.close()

    def test_the_shared_login_cannot_disconnect_or_reconnect_a_bank(self):
        """A disconnect releases a Plaid Item for good and a reconnect
        restarts an MX member's syncing and billing; neither is the shared
        login's to decide. Hiding an account stays open to it."""
        c, _, row = self._household_with_a_bank(waived=True)
        tid = row["tenant_id"]
        for path, kw in (
                ("/api/connections/bank1/disconnect", {}),
                ("/api/connections/mx1/reconnect", {}),
                ("/api/accounts/acct1/remove",
                 {"json": {"mode": "disconnect"}}),
                ("/api/accounts/acct1/remove",
                 {"json": {"mode": "disconnect_purge"}})):
            r = c.post(path, **kw)
            self.assertEqual(r.status_code, 403, f"{path} {kw}: {r.text}")
            self.assertIn("shared demonstration login", r.text, path)
        self.assertEqual(self._item_status(tid, "bank1"), "ok")
        self.assertEqual(self._item_status(tid, "mx1"), "archived")
        r = c.post("/api/accounts/acct1/remove", json={"mode": "hide"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_disconnect_doors_refuse_the_shared_login_themselves(self):
        """The doors refuse it even for a caller that never passed the
        request gate."""
        from fastapi import HTTPException

        from oikonome.web import api
        _, email, row = self._household_with_a_bank(waived=True)
        user = {"user_id": row["id"], "tenant_id": row["tenant_id"],
                "email": email, "role": "owner",
                "second_factor_waived": True}
        for call in (lambda: api.connection_disconnect("bank1", user),
                     lambda: api.connection_reconnect("mx1", user),
                     lambda: api.accounts_remove_api(
                         "acct1", user, {"mode": "disconnect"})):
            with self.assertRaises(HTTPException) as caught:
                call()
            self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self._item_status(row["tenant_id"], "bank1"), "ok")

    def test_an_ordinary_owner_still_disconnects_a_bank(self):
        c, _, row = self._household_with_a_bank(waived=False)
        r = c.post("/api/connections/bank1/disconnect")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._item_status(row["tenant_id"], "bank1"),
                         "archived")

    def test_the_shared_login_cannot_set_or_verify_an_sms_number(self):
        """A number a visitor verifies keeps receiving the household's
        summaries after the visit and after the password's rotation."""
        c, _, row = self._login(waived=True)
        for path, body in (
                ("/api/notify/phone", {"number": "+12175551234",
                                       "consent": True}),
                ("/api/notify/phone", {"number": ""}),
                ("/api/notify/phone/verify", {"code": "123456"})):
            r = c.post(path, json=body)
            self.assertEqual(r.status_code, 403, f"{path}: {r.text}")
            self.assertIn("shared demonstration login", r.text, path)
        ordinary, _, _ = self._login(waived=False)
        r = ordinary.post("/api/notify/phone", json={"number": ""})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_shared_login_cannot_finish_an_owners_plaid_link(self):
        """The link poll is a GET that performs the token exchange; a link
        session an owner of the same household opened is not the shared
        login's to complete."""
        from oikonome.web import pages
        c, _, row = self._login(waived=True)
        token = "lt-" + uuid.uuid4().hex
        pages._PLAID_LINK[token] = {
            "tenant_id": str(row["tenant_id"]), "item_id": None,
            "kind": "add", "institution": None,
            "url": "https://hosted.plaid.com/x", "done": False}
        try:
            r = c.get(f"/accounts/plaid/link/{token}/status")
        finally:
            pages._PLAID_LINK.pop(token, None)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("shared demonstration login", r.text)

    def test_the_plaid_link_helper_refuses_the_shared_login_itself(self):
        """Every Plaid link, web and mobile, starts in one helper; it
        refuses the shared login even for a caller that never passed the
        request gate."""
        from fastapi import HTTPException

        from oikonome.web import pages
        _, email, row = self._login(waived=True)
        user = {"user_id": row["id"], "tenant_id": row["tenant_id"],
                "email": email, "role": "owner",
                "second_factor_waived": True}
        with self.assertRaises(HTTPException) as caught:
            pages._start_plaid_session(user)
        self.assertEqual(caught.exception.status_code, 403)

    def test_the_enrich_door_refuses_the_shared_login_itself(self):
        """The Enrich run refuses the shared login even for a caller that
        never passed the request gate."""
        from fastapi import HTTPException

        from oikonome.web import api
        _, email, row = self._login(waived=True)
        user = {"user_id": row["id"], "tenant_id": row["tenant_id"],
                "email": email, "role": "owner",
                "second_factor_waived": True}
        with self.assertRaises(HTTPException) as caught:
            api.enrich_run_api(user=user, body={"limit": 5})
        self.assertEqual(caught.exception.status_code, 403)

    def test_an_ordinary_owner_still_reaches_the_credential_doors(self):
        # only the doors that answer without calling an aggregator: the
        # rest would reach out to Plaid, MX or SimpleFIN with fake keys
        c, _, _ = self._login(waived=False)
        r = c.post("/api/accounts/mx/connect")
        self.assertNotEqual(r.status_code, 403, r.text)
        r = c.post("/api/accounts/coinbase/link", json={
            "label": "mine", "key_name": "organizations/x/apiKeys/y",
            "private_key": "not-a-real-key"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_mute_delta_names_only_the_shared_logins_own_address(self):
        """The one-address mute toggles are clamped like the recipient
        deltas: the shared login may mute or un-mute itself, never a member
        it was not shown."""
        c, email, other, tid = self._household_with_another_member()
        r = c.post("/api/settings", json={"email_muted_remove": [other]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(other, self._stored(tid).get("email_muted") or [])
        r = c.post("/api/settings", json={
            "email_muted_add": ["someone-else@example.dev", email]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(self._stored(tid)["email_muted"]),
                         sorted([email, other]))
        r = c.post("/api/settings", json={"email_muted_remove": [email]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored(tid)["email_muted"], [other])

    def test_the_whole_mute_list_cannot_silence_a_member_it_was_not_shown(self):
        """The full-list mute key is clamped like the one-address toggles:
        an empty list leaves every other member's mute alone, and naming a
        member the shared login was never shown does not mute them."""
        c, email, other, tid = self._household_with_another_member()
        third = f"t-{uuid.uuid4().hex[:6]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, 'x', 'member', now())",
                (tid, third))
        finally:
            admin.close()
        r = c.post("/api/settings", json={"email_muted": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored(tid)["email_muted"], [other])
        r = c.post("/api/settings", json={"email_muted": [third, email]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(self._stored(tid)["email_muted"]),
                         sorted([email, other]))

    def _push(self):
        return {"endpoint": "https://fcm.googleapis.com/fcm/send/"
                            + uuid.uuid4().hex,
                "keys": {"p256dh": "p" * 20, "auth": "a" * 12}}

    def test_the_shared_login_cannot_subscribe_a_browser_to_push(self):
        """A web-push subscription is a channel to one visitor's browser
        that outlives the visit and the password's rotation."""
        c, _, row = self._login(waived=True)
        r = c.post("/api/notify/push/subscribe", json=self._push())
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("shared demonstration login", r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM push_subscriptions "
            "WHERE tenant_id=%s", row["tenant_id"]), 0)
        ordinary, _, _ = self._login(waived=False)
        # an ordinary login adds a push destination after a step-up
        r = ordinary.post("/api/notify/push/subscribe",
                          json={**self._push(), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def _device(self, c):
        minted = c.post("/api/devices", json={
            "device_name": "visitor phone", "platform": "android"})
        self.assertEqual(minted.status_code, 200, minted.text)
        return TestClient(self.app), {
            "Authorization": f"Bearer {minted.json()['token']}"}

    def test_the_shared_login_cannot_register_a_phone_for_push(self):
        """A native push token carries the household's notices to the
        visitor's phone for as long as the device row lives — the phone
        twin of the browser subscription. The phone itself still signs in
        and still gets its widget (a read it polls, not a channel the
        household sends down), and clearing a token stays open."""
        c, _, row = self._login(waived=True)
        bare, hdr = self._device(c)
        r = bare.post("/api/devices/push", headers=hdr,
                      json={"token": "fcm-token-" + "x" * 20,
                            "platform": "fcm"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("shared demonstration login", r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM device_tokens WHERE user_id=%s "
            "AND push_token IS NOT NULL", row["id"]), 0)
        r = bare.post("/api/devices/push", headers=hdr, json={"token": None})
        self.assertEqual(r.status_code, 200, r.text)
        r = bare.post("/api/devices/widget", headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        ordinary, _, _ = self._login(waived=False)
        bare, hdr = self._device(ordinary)
        r = bare.post("/api/devices/push", headers=hdr,
                      json={"token": "fcm-token-" + "y" * 20,
                            "platform": "fcm"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_an_ordinary_owner_still_mints_invites(self):
        c, _, _ = self._login(waived=False)
        r = c.post("/api/invites", json={"password": PW, "role": "member",
                                         "label": "kid@example.dev"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(c.get("/api/users").json()["users"]), 1)


if __name__ == "__main__":
    unittest.main()
