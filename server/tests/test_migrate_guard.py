"""Migrate's 'apppass' fallback is a LOCAL-dev convenience.

With OIKONOME_APP_PASSWORD unset, an ungated fallback would ALTER the app
role's password to the publicly known default wherever migrate.run() is
pointed — including a remote/production database. The fallback is gated on
the connection being local; _local_host carries the decision, pinned here.
(The refusal path itself needs a non-local database, so the suite pins
the predicate; the local-allow path is exercised by every other test via
_ensure_db, which runs migrate.run() against 127.0.0.1 with the var
unset.)
"""

import unittest

from oikonome.db import migrate


class LocalHostGuardTests(unittest.TestCase):
    def test_local_hosts_allowed(self):
        # dev loopbacks and unix sockets (psycopg reports the socket DIR)
        for host in (None, "127.0.0.1", "::1", "localhost",
                     "/var/run/postgresql", "/tmp"):
            self.assertTrue(migrate._local_host(host), host)

    def test_remote_hosts_refused(self):
        # compose's service name, LAN addresses, real hostnames — anything
        # that could be a shared/production database
        for host in ("postgres", "db.internal", "10.0.0.5",
                     "prod-db.example.com", "192.168.1.20"):
            self.assertFalse(migrate._local_host(host), host)


if __name__ == "__main__":
    unittest.main()
