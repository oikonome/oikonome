"""A claimed household member must not be born email-verified on hosted,
and a household must not grow without bound off share links.

An invite is a bearer link: the claimer types ANY address, and nothing in
the claim proves mailbox control. On hosted, a member row stamped
verified_at at claim time would become a recipient of the daily financial
email — a stranger's address typed into the claim form would get the
household's balances. Hosted claims start unverified (the emailed link
stamps verified_at, exactly like signup); self-host keeps the
trusted-household default.

Claims are also pre-auth — mint is step-up-gated, but
one leaked link plus re-mints could stuff a tenant with accounts. The cap
is enforced under a per-tenant advisory lock inside the claim transaction,
because the existing per-EMAIL lock does not serialize two claims with
different addresses.
"""

import contextlib
import os
import threading
import unittest
import uuid

import psycopg
from psycopg.rows import dict_row

from oikonome.auth import email_verify, invites
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"
_WAIT = 3.0


def _control():
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class _PauseAfterMemberCount:
    """Connection proxy that parks the caller at a barrier right after the
    member-count read — after the cap check's SELECT, before the INSERT.
    Serialized by the tenant lock, only one claimer can be inside that
    window, so the barrier times out; unserialized, both arrive together
    and interleave exactly like two concurrent requests."""

    def __init__(self, conn, barrier):
        self._conn = conn
        self._barrier = barrier

    def execute(self, sql, *a, **kw):
        cur = self._conn.execute(sql, *a, **kw)
        if "SELECT count(*) AS n FROM users" in sql:
            with contextlib.suppress(threading.BrokenBarrierError):
                self._barrier.wait(timeout=_WAIT)
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)


class InviteClaimVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self._hosted = os.environ.pop("OIKONOME_HOSTED", None)
        self.addCleanup(self._restore_hosted)
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(
                admin, f"claimver-{uuid.uuid4().hex[:8]}")
            self.owner = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, %s, 'owner') RETURNING id",
                (self.tid, f"own-{uuid.uuid4().hex[:8]}@example.dev",
                 "x")).fetchone()["id"]
        finally:
            admin.close()
        self.cc = _control()
        self.addCleanup(self.cc.close)

    def _restore_hosted(self):
        if self._hosted is None:
            os.environ.pop("OIKONOME_HOSTED", None)
        else:
            os.environ["OIKONOME_HOSTED"] = self._hosted

    def _claim(self, **kw):
        # The invite NAMES the address that claims it: a hosted invite is
        # addressed and mailed to one mailbox, and only that address may
        # claim it (see test_invite_claims_only_the_named_address).
        email = f"member-{uuid.uuid4().hex[:8]}@example.dev"
        token = invites.create(self.cc, self.tid, self.owner,
                               label=email)["token"]
        return invites.claim(self.cc, token, email, PW, **kw)

    def _verified_at(self, user_id):
        return self.cc.execute(
            "SELECT verified_at FROM users WHERE id=%s",
            (user_id,)).fetchone()["verified_at"]

    def test_hosted_claim_starts_unverified_with_a_working_link(self):
        """On hosted, nothing proved the typed address receives mail, so
        the member starts unverified — and the returned token must be a
        real, consumable verification link so they CAN become verified."""
        os.environ["OIKONOME_HOSTED"] = "1"
        r = self._claim()
        self.assertIsNone(self._verified_at(r["user_id"]))
        self.assertTrue(r["verify_token"])
        row = email_verify.lookup(self.cc, r["verify_token"])
        self.assertIsNotNone(row, "the minted verify token must be live")
        self.assertEqual(row["user_id"], r["user_id"])
        admin = tenancy.admin_connect()
        try:
            self.assertTrue(email_verify.consume(admin, row["id"],
                                                 r["user_id"]))
        finally:
            admin.close()
        self.assertIsNotNone(self._verified_at(r["user_id"]),
                             "the emailed link must complete verification")

    def test_selfhost_claim_stays_verified(self):
        """Self-host is a trusted household with operator-typed addresses
        (the /api/signup contract): the member is verified at birth and no
        verification token is minted."""
        r = self._claim()
        self.assertIsNotNone(self._verified_at(r["user_id"]))
        self.assertIsNone(r["verify_token"])

    def test_explicit_kwarg_overrides_the_environment(self):
        """A caller that has already proven mailbox control may force
        either behaviour regardless of the deployment flag."""
        os.environ["OIKONOME_HOSTED"] = "1"
        r = self._claim(mark_verified=True)
        self.assertIsNotNone(self._verified_at(r["user_id"]))
        del os.environ["OIKONOME_HOSTED"]
        r = self._claim(mark_verified=False)
        self.assertIsNone(self._verified_at(r["user_id"]))
        self.assertTrue(r["verify_token"])


class InviteClaimMemberCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(
                admin, f"claimcap-{uuid.uuid4().hex[:8]}")
            self.owner = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, %s, 'owner') RETURNING id",
                (self.tid, f"own-{uuid.uuid4().hex[:8]}@example.dev",
                 "x")).fetchone()["id"]
        finally:
            admin.close()
        self.cc = _control()
        self.addCleanup(self.cc.close)

    def _fill_to(self, n):
        """Direct-insert members until the tenant has n users total."""
        have = self._count()
        for _ in range(n - have):
            self.cc.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, %s, 'viewer')",
                (self.tid, f"fill-{uuid.uuid4().hex[:8]}@example.dev", "x"))

    def _count(self):
        return self.cc.execute(
            "SELECT count(*) AS n FROM users WHERE tenant_id=%s",
            (self.tid,)).fetchone()["n"]

    def test_full_household_refuses_a_claim(self):
        """At the cap, a claim is refused with the household-full answer —
        for ANY address, so fullness can't probe whether an email has an
        account — and no user row is created. The invite is spent (the
        owner mints a fresh link after freeing a slot)."""
        self._fill_to(invites.MEMBER_CAP)
        late = f"late-{uuid.uuid4().hex[:8]}@example.dev"
        inv = invites.create(self.cc, self.tid, self.owner, label=late)
        with self.assertRaises(ValueError) as ctx:
            invites.claim(self.cc, inv["token"], late, PW)
        self.assertIn("maximum number of members", str(ctx.exception))
        self.assertEqual(self._count(), invites.MEMBER_CAP)
        used = self.cc.execute(
            "SELECT used_at FROM invites WHERE tenant_id=%s",
            (self.tid,)).fetchone()["used_at"]
        self.assertIsNotNone(used, "a cap-refused claim spends the invite")

    def test_one_slot_left_admits_exactly_one_of_two_racers(self):
        """Two concurrent claims with DIFFERENT addresses at cap-1: the
        per-email lock does not serialize them, so without the tenant lock
        both would read count=cap-1 and both insert. The barrier parks a
        claimer right after the count read; serialized, only one can be in
        that window (the barrier times out) and the loser sees a full
        household."""
        self._fill_to(invites.MEMBER_CAP - 1)
        racers = [f"race-{uuid.uuid4().hex[:8]}@example.dev"
                  for _ in range(2)]
        toks = [invites.create(self.cc, self.tid, self.owner,
                               label=e)["token"] for e in racers]
        barrier = threading.Barrier(2)
        results, errors = [], []

        def claim(token, email):
            conn = _control()
            try:
                results.append(invites.claim(
                    _PauseAfterMemberCount(conn, barrier), token,
                    email, PW))
            except Exception as e:               # noqa: BLE001
                errors.append(e)
            finally:
                conn.close()

        threads = [threading.Thread(target=claim, args=(t, e))
                   for t, e in zip(toks, racers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(results), 1,
                         f"expected exactly one admitted member, got "
                         f"{len(results)} ({errors})")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertIn("maximum number of members", str(errors[0]))
        self.assertEqual(self._count(), invites.MEMBER_CAP,
                         "the cap race let both claims insert")




class InviteClaimRouteMailsTheLinkTests(unittest.TestCase):
    """The route must actually SEND the link claim() mints: an unverified
    member who never receives mail is locked out of ever verifying (the
    banner's resend exists, but the first mail must not depend on it)."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        self._hosted = os.environ.pop("OIKONOME_HOSTED", None)
        self.addCleanup(self._restore_hosted)

    def _restore_hosted(self):
        if self._hosted is None:
            os.environ.pop("OIKONOME_HOSTED", None)
        else:
            os.environ["OIKONOME_HOSTED"] = self._hosted

    def _claim_via_route(self):
        from fastapi.testclient import TestClient
        # Mint tenant + invite directly: the hosted SIGNUP door needs an
        # invite and is not what this test is about — only the claim
        # route's mail side-effect is.
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(
                admin, f"claimroute-{uuid.uuid4().hex[:8]}")
            owner_id = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, %s, 'owner') RETURNING id",
                (tid, f"own-{uuid.uuid4().hex[:8]}@example.dev",
                 "x")).fetchone()["id"]
        finally:
            admin.close()
        member = f"member-{uuid.uuid4().hex[:8]}@example.dev"
        token = invites.create(_control(), tid, owner_id,
                               label=member)["token"]
        sent = threading.Event()
        calls = []

        def fake_deliver(email, vtoken, tenant_id):
            calls.append((email, vtoken, tenant_id))
            sent.set()

        orig = self.appmod._deliver_verification
        self.appmod._deliver_verification = fake_deliver
        self.addCleanup(setattr, self.appmod, "_deliver_verification", orig)
        r = TestClient(self.appmod.app).post("/api/invite/claim", json={
            "token": token, "email": member, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return member, sent, calls

    def test_hosted_claim_mails_the_verification_link(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        member, sent, calls = self._claim_via_route()
        self.assertTrue(sent.wait(_WAIT),
                        "hosted claim must send the verification mail")
        email, vtoken, _tid = calls[0]
        self.assertEqual(email, member)
        row = email_verify.lookup(_control(), vtoken)
        self.assertIsNotNone(row, "the mailed token must be a live link")

    def test_selfhost_claim_sends_no_verification_mail(self):
        member, sent, calls = self._claim_via_route()
        self.assertFalse(sent.wait(0.5))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
