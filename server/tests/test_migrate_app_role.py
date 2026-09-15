"""Rotating OIKONOME_APP_PASSWORD must take effect on an existing DB.

migrate.run() used to CREATE ROLE only if absent, so an operator changing
OIKONOME_APP_PASSWORD in docker/.env restarted into an app that couldn't
log in (or, worse, kept the old password working forever). Every migrate
now ALTERs the role password to the env value (idempotent).

CAUTION: the `oikonome_app` role is CLUSTER-global on the shared dev
postgres (127.0.0.1:5433) — other test databases use it with the dev
password 'apppass'. This test keeps the rotation window as short as
possible and always restores 'apppass' (via the code under test, with a
direct ALTER as a finally-net) so parallel suites never see a broken role.
"""

import os
import unittest
import uuid

import psycopg

from oikonome.db import migrate

from . import util


def _can_login(pw: str) -> bool:
    dsn = f"postgresql://oikonome_app:{pw}@127.0.0.1:5433/{util.TEST_DB}"
    try:
        with psycopg.connect(dsn, connect_timeout=3) as c:
            c.execute("SELECT 1")
        return True
    except psycopg.OperationalError:
        return False


class TestAppRolePasswordRotation(unittest.TestCase):
    def test_rotation_takes_effect_on_existing_database(self):
        util._ensure_db()          # database exists, role exists, schema applied
        admin_dsn = util._admin_dsn(util.TEST_DB)
        # a trust-auth cluster accepts ANY password — the negative assertion
        # below would be meaningless there, so detect it up front
        trust_auth = _can_login(f"definitely-wrong-{uuid.uuid4().hex}")
        rotated = f"rotated-{uuid.uuid4().hex[:12]}"
        saved = os.environ.get("OIKONOME_APP_PASSWORD")
        try:
            os.environ["OIKONOME_APP_PASSWORD"] = rotated
            migrate.run(admin_dsn)
            self.assertTrue(_can_login(rotated),
                            "rotated password must work after migrate")
            if not trust_auth:
                self.assertFalse(_can_login("apppass"),
                                 "old password must stop working")
            # rotating BACK goes through the exact same code path — this both
            # proves idempotent re-assertion and restores the shared role
            os.environ["OIKONOME_APP_PASSWORD"] = "apppass"
            migrate.run(admin_dsn)
            self.assertTrue(_can_login("apppass"))
        finally:
            if saved is None:
                os.environ.pop("OIKONOME_APP_PASSWORD", None)
            else:
                os.environ["OIKONOME_APP_PASSWORD"] = saved
            # never leave the cluster-global role rotated, even on failure
            with psycopg.connect(admin_dsn, autocommit=True) as c:
                c.execute("ALTER ROLE oikonome_app WITH LOGIN PASSWORD 'apppass'")


if __name__ == "__main__":
    unittest.main()
