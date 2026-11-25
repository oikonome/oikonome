"""Two invite claims for one email address at once make one member
(specs/invariants-and-fuzzing.md, section 3, seventh scenario).

Each claim burns its own invite, then checks the address is free and
creates the member under the signup lock for that address and the
household's member lock. Run at once, exactly one becomes a member and
the other gets the one generic claim-failed answer, never a database
error, as either serial order gives.
"""

from __future__ import annotations

import unittest

import psycopg
from psycopg.rows import dict_row

from oikonome.auth import invites
from oikonome.db import tenancy

from .concurrency import ScenarioCase, Scenario, Seam

EMAIL_LOCK = Seam("the address's signup lock",
                  sql=r"pg_advisory_xact_lock.* -- oikonome:signup:")
MEMBERS = Seam("the household-member lock", rank=6)
BURN = Seam("the invite's burn", sql=r"UPDATE invites SET used_at")


def _control():
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


def _setup(hh):
    admin = tenancy.admin_connect()
    try:
        owner = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role)"
            " VALUES (%s, %s, 'x', 'owner') RETURNING id",
            (hh.tid, f"own-{hh.tid[:12]}@example.dev")).fetchone()["id"]
    finally:
        admin.close()
    cc = _control()
    try:
        toks = [invites.create(cc, hh.tid, owner)["token"] for _ in range(2)]
    finally:
        cc.close()
    # one address per household: the address is unique across the instance
    return {"tokens": toks, "email": f"claimee-{hh.tid[:12]}@example.dev"}


def _claim(i):
    def claim(hh, ctx):
        conn = _control()
        try:
            return invites.claim(conn, ctx["tokens"][i], ctx["email"],
                                 "correct-horse-battery")
        finally:
            conn.close()
    return claim


def _digest(hh, ctx):
    admin = tenancy.admin_connect()
    try:
        return (
            tuple((r["role"],) for r in admin.execute(
                "SELECT role FROM users WHERE email = %s ORDER BY role",
                (ctx["email"],)).fetchall()),
            admin.execute(
                "SELECT count(*) AS n FROM users WHERE tenant_id = %s",
                (hh.tid,)).fetchone()["n"])
    finally:
        admin.close()


class TwoClaimsWithOneEmail(ScenarioCase, unittest.TestCase):
    def test_two_claims_with_one_email_make_one_member(self):
        self.check(Scenario(
            "two invite claims with one email",
            setup=_setup, actors={"A": _claim(0), "B": _claim(1)},
            seams=(BURN, EMAIL_LOCK, MEMBERS), digest=_digest,
            expected=(ValueError,)))


if __name__ == "__main__":
    unittest.main()
