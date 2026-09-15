"""A squatted address is taken back only after the mailbox proves itself.

Hosted signup inserts the row unverified, so with open signup anyone can
register an address they do not own and enrol a second factor on it. A
signup for such an address creates nothing and opens no session: it parks
the signup under a one-shot token and mails the address. The click (the
POST behind the confirm page) performs the reclaim. A reclaim that acts
without that click is a takeover.

What the click does depends on what the address IS and what the household
HOLDS, and the three answers together are the whole contract:

  * a MEMBER row inside somebody else's household is moved out into a
    household of its own, whatever that household holds — the move-out
    erases nothing, so occupancy has no say in it;
  * an OWNER of a household holding nothing is replaced, and the fresh
    account is created already verified;
  * an OWNER of a household holding ANYTHING — a row, a subscription, or
    another person — is REFUSED. Nothing is erased, nothing is handed
    over, and the answer is to write to a human.

There is deliberately no fourth answer: handing an owner alone in a
records-holding household a password reset against that same account is a
takeover road, however it is guarded — not least because "this account
never proved a mailbox" cannot be read off `verified_at`, which a hosted
email change clears on a household that verified years ago. The cost of
refusing is that a squatter who writes one row keeps an address until an
operator deletes the tenant by hand; that is a support ticket, and every
automated answer to it hands somebody's household to a stranger.

Occupancy is derived from every table the erasure would delete, not from a
list of the interesting ones.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class ReclaimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                                 "OIKONOME_OPEN_SIGNUP": "1"})
        self._env.start()
        self._dev = mock.patch.object(self.appmod, "DEV_MODE", False)
        self._dev.start()
        self._vmail = mock.patch.object(self.appmod, "_deliver_verification")
        self._vmail.start()
        self.sent: list[tuple[str, str]] = []
        self._rmail = mock.patch.object(
            self.appmod, "_deliver_reclaim",
            side_effect=lambda email, token: self.sent.append((email, token)))
        self._rmail.start()
        from oikonome.web import security
        security._limiter._hits.clear()

    def tearDown(self):
        for p in (self._rmail, self._vmail, self._dev, self._env):
            p.stop()

    def _signup(self, email, invite="", ref="", password=PW):
        return TestClient(self.appmod.app).post(
            "/api/signup", data={"email": email, "password": password,
                                 "invite": invite, "ref": ref})

    def _user(self, email):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT u.id, u.tenant_id, u.role, u.verified_at, "
                "u.password_hash FROM users u WHERE u.email=%s",
                (email,)).fetchone()
        finally:
            admin.close()

    def _finish(self, token, password="clicker-chosen-password"):
        c = TestClient(self.appmod.app)
        r = c.post("/signup/reclaim",
                   data={"token": token, "password": password},
                   follow_redirects=False)
        return c, r

    def test_a_taken_unverified_address_parks_the_signup_and_says_nothing(self):
        """The park is invisible in the reply: a taken address answers the
        same 409 whether or not it was ever confirmed, so a prober cannot
        learn that a live reclaim mail is sitting in that inbox — the
        pretext a phishing copy of that mail would need."""
        email = f"sq-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(email).status_code, 200)
        before = self._user(email)
        r = self._signup(email, password="another-good-password")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertNotIn("set-cookie", {k.lower() for k in r.headers})
        self.assertEqual(self._user(email), before)     # nothing changed
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], email)
        # and a VERIFIED address answers identically — no mail, same body
        other = f"ver-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(other).status_code, 200)
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET verified_at=now() WHERE email=%s",
                          (other,)); admin.commit()
        finally:
            admin.close()
        v = self._signup(other)
        self.assertEqual(v.status_code, 409)
        self.assertEqual(v.json(), r.json())
        self.assertEqual(len(self.sent), 1)             # no second mail

    def test_the_click_wipes_an_owner_squat_and_creates_a_verified_account(self):
        """The wipe road, and it is for a shell that holds NOTHING — a
        squatter who has only registered the address and signed in."""
        email = f"own-{uuid.uuid4().hex[:8]}@x.dev"
        first = self._signup(email)
        self.assertEqual(first.status_code, 200)
        sq = self._user(email)
        squatter = TestClient(self.appmod.app, cookies=first.cookies)
        self.assertEqual(self._signup(email, password="owner-real-password")
                         .status_code, 409)
        token = self.sent[-1][1]
        # the GET is a confirm page, not the act
        g = TestClient(self.appmod.app).get(f"/signup/reclaim?token={token}")
        self.assertEqual(g.status_code, 200)
        self.assertEqual(self._user(email)["tenant_id"], sq["tenant_id"])
        c, r = self._finish(token, password="the-clickers-own-password")
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(r.headers["location"], "/app/welcome")
        me = c.get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        after = self._user(email)
        self.assertNotEqual(after["tenant_id"], sq["tenant_id"])
        self.assertIsNotNone(after["verified_at"])
        self.assertEqual(after["role"], "owner")
        self.assertEqual(squatter.get("/api/me").status_code, 401)
        admin = tenancy.admin_connect()
        try:
            self.assertIsNone(admin.execute(
                "SELECT 1 FROM tenants WHERE id=%s", (sq["tenant_id"],)).fetchone())
        finally:
            admin.close()
        # the password is the CLICKER's, never the one typed on the form
        # that produced the link — that form may have been filled by
        # someone who does not own this mailbox
        stale = TestClient(self.appmod.app).post(
            "/api/login", data={"email": email,
                                "password": "owner-real-password"})
        self.assertEqual(stale.status_code, 401, stale.text)
        login = TestClient(self.appmod.app).post(
            "/api/login", data={"email": email,
                                "password": "the-clickers-own-password"})
        self.assertEqual(login.status_code, 200, login.text)
        # one use
        _, again = self._finish(token)
        self.assertEqual(again.status_code, 400)

    def test_the_click_moves_a_member_squat_out_of_the_strangers_household(self):
        host = f"host-{uuid.uuid4().hex[:8]}@x.dev"
        victim = f"vic-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(host).status_code, 200)
        h = self._user(host)
        admin = tenancy.admin_connect()
        try:
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member') RETURNING id",
                (h["tenant_id"], victim)).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        self.assertEqual(self._signup(victim).status_code, 409)
        c, r = self._finish(self.sent[-1][1])
        self.assertEqual(r.status_code, 303, r.text)
        v = self._user(victim)
        self.assertEqual(v["id"], uid)                    # the row moved
        self.assertNotEqual(v["tenant_id"], h["tenant_id"])
        self.assertEqual(v["role"], "owner")
        self.assertIsNotNone(v["verified_at"])
        admin = tenancy.admin_connect()
        try:
            n = admin.execute("SELECT count(*) AS n FROM users WHERE tenant_id=%s",
                              (h["tenant_id"],)).fetchone()["n"]
        finally:
            admin.close()
        self.assertEqual(n, 1)                             # host intact



    def test_the_confirm_page_refuses_a_weak_or_missing_password(self):
        email = f"pw-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(email).status_code, 200)
        self.assertEqual(self._signup(email).status_code, 409)
        token = self.sent[-1][1]
        _, r = self._finish(token, password="short")
        self.assertEqual(r.status_code, 400)
        # and the token survives a refusal — the person can try again
        _, ok = self._finish(token)
        self.assertEqual(ok.status_code, 303, ok.text)

    def _seed_records(self, tenant_id):
        """Make the household a REAL one. This is the ordinary state of an
        owner whose verification mail went to spam: unverified, and using
        the app the whole time."""
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO transactions (tenant_id, id, date, amount, "
                "name) VALUES (%s, %s, now()::date, 12.50, 'Groceries')",
                (tenant_id, f"tx-{uuid.uuid4().hex[:8]}"))
            admin.commit()
        finally:
            admin.close()

    def _txn_count(self, tenant_id):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT count(*) AS n FROM transactions WHERE tenant_id=%s",
                (tenant_id,)).fetchone()["n"]
        finally:
            admin.close()

    def _resets_for(self, user_id) -> int:
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT count(*) AS n FROM password_resets WHERE user_id=%s",
                (user_id,)).fetchone()["n"]
        finally:
            admin.close()

    def test_a_household_that_holds_records_is_never_touched(self):
        """Unverified is not empty. The wipe releases what the household
        holds at external services before it drops the rows, so no backup
        brings such a household back — and the only thing standing between
        it and the click was one clause on a confirm page.

        A household that holds records keeps every row it has, and the
        click does NOTHING ELSE either: no password reset, no session, no
        token spent. The contract is that this door has no automated answer
        for an in-use household."""
        email = f"live-{uuid.uuid4().hex[:8]}@x.dev"
        first = self._signup(email)
        self.assertEqual(first.status_code, 200)
        live = self._user(email)
        self._seed_records(live["tenant_id"])
        self.assertEqual(self._signup(email).status_code, 409)
        token = self.sent[-1][1]
        # the confirm page says the button will not act, before it exists
        g = TestClient(self.appmod.app).get(f"/signup/reclaim?token={token}")
        self.assertEqual(g.status_code, 200)
        self.assertIn("will be deleted", g.text.lower())
        self.assertIn("will be handed over", g.text.lower())
        self.assertIn("whoever runs this instance", g.text)
        self.assertNotIn("<form", g.text)
        # and the POST refuses: nothing erased, nothing reset, no session
        c, r = self._finish(token)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertNotIn("location", {k.lower() for k in r.headers})
        self.assertNotIn("set-cookie", {k.lower() for k in r.headers})
        after = self._user(email)
        self.assertEqual(after["tenant_id"], live["tenant_id"])
        self.assertEqual(after["id"], live["id"])
        self.assertEqual(after["password_hash"], live["password_hash"])
        self.assertIsNone(after["verified_at"])
        self.assertEqual(self._txn_count(live["tenant_id"]), 1)
        # no reset link was minted — that road is gone, not merely unused
        self.assertEqual(self._resets_for(live["id"]), 0)
        # refusing spends nothing: the link is as valid — and as inert —
        # as it was, so a support case is not also a dead link
        _, again = self._finish(token)
        self.assertEqual(again.status_code, 409, again.text)

    def test_bills_a_budget_and_a_partner_are_records_too(self):
        """Occupancy is everything the erasure would delete, not the five
        things somebody thought to list. A household used as a manual
        budget tracker has no bank connection, no transaction and no
        subscription — and it has hand-entered bills, a budget that lives
        in the settings config, and an invited partner whose password and
        passkeys the erasure's cascade would take with it.

        Each of the three is enough on its own to make the household in
        use, and in-use has exactly one answer: refuse."""
        for seed in ("bills", "config", "partner"):
            with self.subTest(seed=seed):
                from oikonome.web import security
                security._limiter._hits.clear()   # 6 signups, 5/hr per IP
                email = f"{seed}-{uuid.uuid4().hex[:8]}@x.dev"
                self.assertEqual(self._signup(email).status_code, 200)
                live = self._user(email)
                admin = tenancy.admin_connect()
                try:
                    if seed == "bills":
                        admin.execute(
                            "INSERT INTO bills (tenant_id, id, type, payee, "
                            "amount, frequency) VALUES (%s, 'rec:rent', "
                            "'BILL', 'Landlord', -1200, 'monthly')",
                            (live["tenant_id"],))
                    elif seed == "config":
                        admin.execute(
                            "UPDATE tenant_settings SET config = config || "
                            "'{\"savings_goals\": [{\"name\": \"Roof\"}]}'"
                            "::jsonb WHERE tenant_id=%s",
                            (live["tenant_id"],))
                    else:
                        admin.execute(
                            "INSERT INTO users (tenant_id, email, "
                            "password_hash, role) VALUES (%s, %s, 'x', "
                            "'member')",
                            (live["tenant_id"],
                             f"partner-{uuid.uuid4().hex[:6]}@x.dev"))
                    admin.commit()
                finally:
                    admin.close()
                self.assertEqual(self._signup(email).status_code, 409)
                _, r = self._finish(self.sent[-1][1])
                self.assertEqual(r.status_code, 409, r.text)
                self.assertEqual(self._user(email)["tenant_id"],
                                 live["tenant_id"])
                self.assertEqual(self._resets_for(live["id"]), 0)

    def test_a_second_factor_alone_never_makes_a_squat_unreclaimable(self):
        """Enrolling a factor on an address you do not own is exactly how a
        squat is held — it is why this door exists. Occupancy is records,
        money and other people, never the factor."""
        email = f"squat-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(email).status_code, 200)
        sq = self._user(email)
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, 'x', 0)",
                (sq["id"], f"cred-{uuid.uuid4().hex}"))
            admin.commit()
        finally:
            admin.close()
        self.assertEqual(self._signup(email).status_code, 409)
        _, r = self._finish(self.sent[-1][1])
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(r.headers["location"], "/app/welcome")
        self.assertNotEqual(self._user(email)["tenant_id"], sq["tenant_id"])

    def test_a_squatter_who_writes_one_row_keeps_the_address_until_a_human_looks(self):
        """The cost of refusing, stated as a test so it cannot be
        rediscovered as a surprise.

        A squatter who creates ONE row makes the household "in use", and
        an in-use household is refused. So the address stays with the
        squatter: the person who reads the mail gets a support address,
        not an account. That is a worse product than an automatic reset
        promises — and an automatic reset hands somebody's household to a
        stranger. A support ticket is recoverable; the takeover is not.

        What this test pins is that the refusal is COMPLETE. The click
        must not half-act: no reset link minted, no factor stripped, no
        session of the existing account killed, and the squatter's row
        still there afterwards. A partial act would be the worst of both.
        """
        email = f"held-{uuid.uuid4().hex[:8]}@x.dev"
        first = self._signup(email)
        self.assertEqual(first.status_code, 200)
        sq = self._user(email)
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO accounts (tenant_id, id, name, type) "
                "VALUES (%s, %s, 'Squat', 'depository')",
                (sq["tenant_id"], f"acct-{uuid.uuid4().hex[:8]}"))
            admin.execute("UPDATE users SET totp_secret='x' WHERE id=%s",
                          (sq["id"],))
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, 'x', 0)",
                (sq["id"], f"cred-{uuid.uuid4().hex}"))
            admin.commit()
        finally:
            admin.close()
        self.assertEqual(self._signup(email).status_code, 409)
        _, r = self._finish(self.sent[-1][1])
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("whoever runs this instance", r.text)
        # nothing about the existing account moved
        self.assertEqual(self._resets_for(sq["id"]), 0)
        admin = tenancy.admin_connect()
        try:
            row = admin.execute(
                "SELECT totp_secret, verified_at, (SELECT count(*) FROM "
                "passkeys WHERE user_id=%s) AS n_pk FROM users WHERE id=%s",
                (sq["id"], sq["id"])).fetchone()
        finally:
            admin.close()
        self.assertEqual(row["totp_secret"], "x")
        self.assertEqual(row["n_pk"], 1)
        self.assertIsNone(row["verified_at"])
        self.assertEqual(self._user(email)["password_hash"],
                         sq["password_hash"])
        # and the squatter's own session is untouched — the door did
        # nothing, which is exactly what "refuse" has to mean
        self.assertEqual(TestClient(self.appmod.app, cookies=first.cookies)
                         .get("/api/me").status_code, 200)

    def test_a_member_is_moved_out_of_a_household_that_holds_records(self):
        """The move-out road erases NOTHING, so what the other household
        holds has no say over it.

        Anyone with a household can mint a family invite and claim it
        themselves as an address they do not own — /api/invite/claim takes
        any address and asks for no mailbox proof — which plants an
        unverified member row for the victim inside the attacker's tenant.
        Gate the move-out on occupancy and the attacker's own transactions
        make that household "in use", so the victim's click becomes a
        password reset INSIDE it: they read the attacker's ledger, and
        every account they link afterwards syncs into the tenant whose
        owner session the attacker still holds.

        The occupancy test protects a household from ERASURE. A member
        moving out erases nothing — the household keeps every row, and the
        person leaving is the one who just proved the mailbox."""
        host = f"livehost-{uuid.uuid4().hex[:8]}@x.dev"
        member = f"livemem-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(host).status_code, 200)
        h = self._user(host)
        self._seed_records(h["tenant_id"])
        admin = tenancy.admin_connect()
        try:
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member') RETURNING id",
                (h["tenant_id"], member)).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        self.assertEqual(self._signup(member).status_code, 409)
        token = self.sent[-1][1]
        # the confirm page offers the move-out, not a password reset into
        # somebody else's household
        g = TestClient(self.appmod.app).get(f"/signup/reclaim?token={token}")
        self.assertEqual(g.status_code, 200)
        self.assertIn("someone else's household", g.text)
        self.assertIn("Finish and sign in", g.text)
        self.assertNotIn("Set a new password", g.text)
        c, r = self._finish(token, password="the-members-own-pw")
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(r.headers["location"], "/app/welcome")
        after = self._user(member)
        self.assertEqual(after["id"], uid)                  # the row moved
        self.assertNotEqual(after["tenant_id"], h["tenant_id"])
        self.assertEqual(after["role"], "owner")
        self.assertIsNotNone(after["verified_at"])
        # the household they left keeps everything, and keeps only its owner
        self.assertEqual(self._txn_count(h["tenant_id"]), 1)
        admin = tenancy.admin_connect()
        try:
            n = admin.execute(
                "SELECT count(*) AS n FROM users WHERE tenant_id=%s",
                (h["tenant_id"],)).fetchone()["n"]
        finally:
            admin.close()
        self.assertEqual(n, 1)
        # and the session the click opened is in the NEW household
        me = c.get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(self._txn_count(after["tenant_id"]), 0)

    def test_a_household_with_other_people_in_it_is_refused_not_handed_over(
            self):
        """The reset road rewrites ONE user id's credentials and nothing
        else, so it is a handover only when that user is the household's
        only principal.

        A squatter registers the victim's address, then mints a family
        invite and claims it themselves as an accomplice — again, no
        mailbox proof anywhere. The tenant now holds a second users row,
        which makes it "in use", which sends the victim's click down the
        reset road. The WIPE was the only path that removed co-tenants, so
        the accomplice would keep their own password, sessions, passkeys,
        device tokens, push subscriptions and script tokens inside the
        household the victim now believes is theirs — and household roles
        read everything.

        Revoking the accomplice's persistence and carrying on would be one
        more clever action on a door where every clever action has been a
        takeover. This refuses instead: a squatter who plants an accomplice
        costs a human decision, not an automated takeover."""
        email = f"shared-{uuid.uuid4().hex[:8]}@x.dev"
        first = self._signup(email)
        self.assertEqual(first.status_code, 200)
        live = self._user(email)
        accomplice = f"acc-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            acc_uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member') RETURNING id",
                (live["tenant_id"], accomplice)).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        self.assertEqual(self._signup(email).status_code, 409)
        token = self.sent[-1][1]
        # the confirm page has no button at all — it says so before anyone
        # presses anything
        g = TestClient(self.appmod.app).get(f"/signup/reclaim?token={token}")
        self.assertEqual(g.status_code, 200)
        self.assertIn("will be handed over", g.text.lower())
        self.assertIn("whoever runs this instance", g.text)
        self.assertNotIn("<form", g.text)
        # and the click does nothing: no reset, no session, no move
        c, r = self._finish(token)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertNotIn("location", {k.lower() for k in r.headers})
        self.assertNotIn("set-cookie", {k.lower() for k in r.headers})
        self.assertIn("whoever runs this instance", r.text)
        after = self._user(email)
        self.assertEqual(after["tenant_id"], live["tenant_id"])
        self.assertEqual(after["password_hash"], live["password_hash"])
        self.assertIsNone(after["verified_at"])
        admin = tenancy.admin_connect()
        try:
            self.assertIsNotNone(admin.execute(
                "SELECT 1 FROM users WHERE id=%s", (acc_uid,)).fetchone())
            self.assertIsNone(admin.execute(
                "SELECT 1 FROM password_resets WHERE user_id=%s",
                (live["id"],)).fetchone())
        finally:
            admin.close()
        # refusing spends nothing: the link is as valid — and as inert —
        # as it was, so a support case is not also a dead link
        _, again = self._finish(token)
        self.assertEqual(again.status_code, 409, again.text)

    def _change_address(self, user_id, new_email):
        """What the hosted email-change door does to the users row: the
        address moves and `verified_at` is cleared, because the new
        mailbox has not been proved yet."""
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE users SET email=%s, verified_at=NULL WHERE id=%s",
                (new_email, user_id))
            admin.commit()
        finally:
            admin.close()

    def test_a_household_that_changed_its_address_is_never_handed_over(self):
        """`verified_at IS NULL` does not mean "never proved a mailbox".

        A hosted email change clears it, because the NEW address is
        unproven — so a household that verified years ago, holds years of
        records and has a second factor enrolled reads exactly like a
        fresh squat to anything that tests `verified_at` alone. A door
        that tested it that way would turn the mail sent to the new address
        into a reset that strips every factor without asking for a recovery
        code: whoever receives mail there would own the household.

        This is the whole reason there is no reset road. The click on such
        an address must refuse, and the account must come through it
        completely unchanged."""
        old_addr = f"moved-{uuid.uuid4().hex[:8]}@x.dev"
        new_addr = f"newbox-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(old_addr).status_code, 200)
        u = self._user(old_addr)
        self._seed_records(u["tenant_id"])
        admin = tenancy.admin_connect()
        try:
            # a long-verified household, with a factor its owner enrolled
            admin.execute("UPDATE users SET verified_at=now(), "
                          "totp_secret='real-owners-secret' WHERE id=%s",
                          (u["id"],))
            admin.commit()
        finally:
            admin.close()
        self._change_address(u["id"], new_addr)
        # a stranger who reads the new mailbox starts a signup for it
        from oikonome.web import security
        security._limiter._hits.clear()
        self.assertEqual(self._signup(new_addr).status_code, 409)
        token = self.sent[-1][1]
        g = TestClient(self.appmod.app).get(f"/signup/reclaim?token={token}")
        self.assertEqual(g.status_code, 200)
        self.assertNotIn("<form", g.text)
        _, r = self._finish(token)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertNotIn("set-cookie", {k.lower() for k in r.headers})
        self.assertEqual(self._resets_for(u["id"]), 0)
        admin = tenancy.admin_connect()
        try:
            row = admin.execute("SELECT totp_secret FROM users WHERE id=%s",
                                (u["id"],)).fetchone()
        finally:
            admin.close()
        self.assertEqual(row["totp_secret"], "real-owners-secret")
        self.assertEqual(self._txn_count(u["tenant_id"]), 1)

    def test_a_reclaim_token_dies_when_the_account_moves_to_another_address(
            self):
        """The token speaks for ONE mailbox, and only while the account it
        names still lives there.

        Bound to the user id alone it would not be: `users.email` is
        mutable, so a link minted for old@ would keep resolving to the same
        row after that row moved to new@ — an hour-long, one-click act
        against an address whose holder never received the mail, spendable
        by whoever still holds the old link. The lookup matches the address
        the token was mailed to as well, so the moment the account leaves
        that mailbox the token names nothing."""
        old_addr = f"tokmove-{uuid.uuid4().hex[:8]}@x.dev"
        new_addr = f"tokdest-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(old_addr).status_code, 200)
        u = self._user(old_addr)
        self.assertEqual(self._signup(old_addr).status_code, 409)
        token = self.sent[-1][1]
        self._change_address(u["id"], new_addr)
        g = TestClient(self.appmod.app).get(f"/signup/reclaim?token={token}")
        self.assertEqual(g.status_code, 400, g.text)
        _, r = self._finish(token)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertNotIn("set-cookie", {k.lower() for k in r.headers})
        # the household is exactly where it was, under its new address
        after = self._user(new_addr)
        self.assertIsNotNone(after)
        self.assertEqual(after["id"], u["id"])
        self.assertEqual(after["tenant_id"], u["tenant_id"])
        admin = tenancy.admin_connect()
        try:
            self.assertIsNotNone(admin.execute(
                "SELECT 1 FROM tenants WHERE id=%s",
                (u["tenant_id"],)).fetchone())
        finally:
            admin.close()

    def test_no_reset_link_ever_waives_the_recovery_code(self):
        """One rule for every reset link. reset.consume strips passkeys,
        the TOTP secret and the recovery batch, so an account with a
        strong factor owes one of that factor's recovery codes — with no
        exemption for where the link came from. An exemption decided by
        re-reading account state at /reset is one a state change somewhere
        else (an email change) can silently re-arm."""
        from oikonome.auth import reset as reset_mod
        email = f"owed-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(email).status_code, 200)
        u = self._user(email)
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, 'x', 0)",
                (u["id"], f"cred-{uuid.uuid4().hex}"))
            rt = reset_mod.create_reset(admin, u["id"])
            admin.commit()
        finally:
            admin.close()
        # unverified, factor-carrying, freshly minted — the exact shape a
        # waiver would fire on
        page = TestClient(self.appmod.app).get(f"/reset?token={rt}")
        self.assertIn("recovery", page.text.lower())
        blocked = TestClient(self.appmod.app).post(
            "/reset", data={"token": rt, "password": "a-strangers-password"},
            follow_redirects=False)
        self.assertEqual(blocked.status_code, 401, blocked.text)

    def test_a_failure_after_the_wipe_leaves_a_road_out(self):
        """The wipe commits before the replacement exists, so a failure in
        between must not end with a bare 500 and a person who believes
        their account is in limbo. The address is genuinely free by then,
        and the answer says so — signing up again works."""
        email = f"half-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(email).status_code, 200)
        self.assertEqual(self._signup(email).status_code, 409)
        token = self.sent[-1][1]
        with mock.patch.object(self.appmod, "_provision_household",
                               side_effect=RuntimeError("provisioning fell "
                                                        "over")):
            _, r = self._finish(token)
        self.assertEqual(r.status_code, 500, r.text)
        self.assertIn("start the signup again", r.text)
        self.assertIsNone(self._user(email))
        self.assertEqual(self._signup(email).status_code, 200)

    def test_the_reclaim_mail_is_capped_per_address(self):
        """The route's only throttle is per-IP, so without a per-address
        cap a distributed caller could pour unlimited mail into a chosen
        inbox — our sender's reputation, and a live one-hour destructive
        link kept permanently fresh in front of whoever reads it. Every
        sibling mail door carries the same cap."""
        from oikonome.web import security
        email = f"flood-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(email).status_code, 200)
        for _ in range(6):
            security._limiter._hits.clear()
            self.assertEqual(self._signup(email).status_code, 409)
        self.assertEqual(len(self.sent), self.appmod._RECLAIM_PER_HOUR)
        # and the link already delivered still works — suppressing the
        # send suppresses the MINT, so a flood cannot burn the real one
        _, r = self._finish(self.sent[-1][1])
        self.assertEqual(r.status_code, 303, r.text)

    def test_a_verified_address_is_409_and_a_verified_row_refuses_the_click(self):
        email = f"ver-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(email).status_code, 200)
        self.assertEqual(self._signup(email).status_code, 409)
        token = self.sent[-1][1]
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET verified_at=now() WHERE email=%s",
                          (email,)); admin.commit()
        finally:
            admin.close()
        # verified in the meantime (the owner got in the real way): the
        # parked reclaim must not run — it would be the takeover
        _, r = self._finish(token)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._signup(email).status_code, 409)
