"""Restore and reset drop exactly the database the app uses, and a restore
the app cannot start on is undone.

On an external server the database name from docker/.env is spliced into
DROP/CREATE DATABASE. The app's DSN uses that name literally and case-
sensitively; spliced unquoted, psql folds `Ledger` to `ledger` (another
instance's database on a shared cluster gets force-dropped while the app's
own survives) and a hyphenated name is a syntax error that makes every
restore and reset fail. A name that cannot be a plain identifier is
refused before anything is touched.

A restore drops the live database, loads the dump and starts the app. If
the app then never comes up (a migration that fails on an old dump), the
instance must not be left down on data nothing can run: it is rolled back
to the snapshot the restore took of the database it replaced.
"""

import gzip
import unittest

from . import lifecycle_stub as ls

EXT = ("COMPOSE_PROJECT_NAME=acme\nOIKONOME_PORT=8999\n"
       "COMPOSE_FILE=compose.yaml:compose.external-db.yaml\n"
       "OIKONOME_DB_HOST=db.example.invalid\n")


class ExternalDatabaseNameTests(unittest.TestCase):

    def _reset(self, name_line: str):
        t = ls.tempdir(self)
        inst = ls.stage(t, EXT + name_line + "\n")
        binp = ls.make_bin(t)
        log = t / "log"
        r = ls.bash(f'bash "{inst}/oikonome.sh" reset <<< reset',
                    env=ls.run_env(binp, log))
        return r, (log.read_text() if log.exists() else ""), inst

    def test_the_drop_names_the_database_exactly_as_written(self):
        r, log, _inst = self._reset("OIKONOME_DB_NAME=Ledger-Prod")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('PSQLARG:DROP DATABASE IF EXISTS "Ledger-Prod" WITH (FORCE);',
                      log)
        self.assertIn('PSQLARG:CREATE DATABASE "Ledger-Prod";', log)

    def test_a_name_that_is_not_a_plain_identifier_touches_nothing(self):
        r, log, inst = self._reset('OIKONOME_DB_NAME="x; DROP DATABASE y"')
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("DROP DATABASE", log)
        self.assertTrue((inst / "docker" / ".env").exists())
        self.assertFalse((inst / "install-ran").exists())


LOCAL = "COMPOSE_PROJECT_NAME=acme\nOIKONOME_PORT=8999\n"


def _restore(test, env_text=LOCAL, **knobs):
    t = ls.tempdir(test)
    inst = ls.stage(t, env_text)
    filler = "CREATE TABLE t (x int);\n" * 80
    (inst / "backups" / "oikonome-old.sql.gz").write_bytes(
        gzip.compress(("RESTORED-DUMP\n" + filler).encode()))
    live = t / "live.sql"
    live.write_text("LIVE-SNAPSHOT\n" + filler)
    binp = ls.make_bin(t, no_sleep=True, curl_fails=True)
    log = t / "log"
    r = ls.bash(f'bash "{inst}/oikonome.sh" restore oikonome-old.sql.gz '
                '<<< restore',
                env=ls.run_env(binp, log, STUB_USERS=2,
                               STUB_DUMP_FILE=live, **knobs))
    return r, log.read_text()


class RestoreUndoneWhenTheAppWillNotStartTests(unittest.TestCase):

    def _assert_rolled_back(self, r, log):
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("rolled back", r.stdout)
        loads = log.split("LOAD-BEGIN")[1:]
        self.assertEqual(len(loads), 2, "expected the dump, then the rollback")
        self.assertIn("RESTORED-DUMP", loads[0])
        self.assertIn("LIVE-SNAPSHOT", loads[1])

    def test_a_start_that_fails_outright_is_rolled_back_too(self):
        # the run from a shell is under errexit: the failed `up` (a migrate
        # service that exits non-zero) must reach the rollback, not end the
        # script with the instance down on the restored data
        r, log = _restore(self, STUB_UP_FAIL_AT=2)
        self._assert_rolled_back(r, log)

    def test_the_replaced_database_is_put_back(self):
        r, log = _restore(self)
        self._assert_rolled_back(r, log)


class BundledDatabaseNameTests(unittest.TestCase):
    """OIKONOME_DB_NAME only names an external database. The bundled
    postgres is always loaded and dumped as `oikonome`, so a value left
    over from an external setup must not make a restore drop another name
    while it loads into `oikonome`."""

    def test_a_leftover_name_does_not_redirect_the_bundled_drop(self):
        r, log = _restore(self, LOCAL + "OIKONOME_DB_NAME=Ledger\n")
        drops = [ln for ln in log.splitlines() if ln.startswith("DROPSQL:")]
        self.assertTrue(drops, r.stdout + r.stderr)
        for ln in drops:
            self.assertIn('DROP DATABASE IF EXISTS "oikonome"', ln)
            self.assertNotIn("Ledger", ln)
        self.assertIn("rolled back", r.stdout)


if __name__ == "__main__":
    unittest.main()
