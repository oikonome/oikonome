"""The Postgres major-version migration never mistakes "cannot read the
folder" for "no database", and never puts a secret on a command line.

Under rootful Docker the postgres entrypoint hands the data folder to the
container's uid at mode 0700. The usual operator — a docker-group user
without passwordless sudo, or the console's host agent — cannot read its
PG_VERSION. Read as "fresh install, nothing to migrate", the upgrade starts
the new major on the old cluster and postgres crash-loops. The version is
read through a container instead, and if even that fails the migration
stops loudly with nothing changed.

Secrets travel in the environment, never in argv, where every account on
the host can read them in ps/proc for as long as the client runs.
"""

import os
import re
import stat
import unittest

from . import lifecycle_stub as ls

ROOT = ls.ROOT


@unittest.skipIf(os.geteuid() == 0, "root reads a 0700 folder anyway")
class UnreadableClusterTests(unittest.TestCase):

    def _run(self, **knobs):
        t = ls.tempdir(self)
        inst = ls.stage(t, "COMPOSE_PROJECT_NAME=acme\n", with_compose=True)
        data = inst / "data" / "postgres"
        data.mkdir(parents=True)
        (data / "PG_VERSION").write_text("16\n")
        data.chmod(0)
        self.addCleanup(data.chmod, stat.S_IRWXU)
        binp = ls.make_bin(t)
        log = t / "log"
        r = ls.bash(f'bash "{inst}/scripts/pg-major-migrate.sh" "{inst}"',
                    env=ls.run_env(binp, log, **knobs))
        return r, log.read_text() if log.exists() else ""

    def test_a_probe_that_cannot_read_it_either_stops_the_upgrade(self):
        r, _log = self._run(STUB_RUN_RC=1)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("can't read which Postgres version", r.stderr)

    def test_the_version_is_read_through_a_container(self):
        want = re.search(r"image: docker.io/library/postgres:(\d+)",
                         (ROOT / "docker/compose.yaml").read_text()).group(1)
        r, log = self._run(STUB_RUN_OUT=want)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"postgres:{want}-alpine", log)
        self.assertIn("/d:ro", log)


class NoSecretOnACommandLineTests(unittest.TestCase):
    SECRET_IN_ARGV = re.compile(
        r"-e\s+\"?[A-Z_]*(PASSWORD|KEY|SECRET|TOKEN)=")

    def test_no_lifecycle_script_passes_a_secret_by_value_to_an_engine(self):
        files = [ROOT / "oikonome.sh", ROOT / "install.sh",
                 ROOT / "uninstall.sh", *sorted((ROOT / "scripts").glob("*.sh"))]
        for f in files:
            for n, line in enumerate(f.read_text().splitlines(), 1):
                with self.subTest(file=f.name, line=n):
                    self.assertIsNone(self.SECRET_IN_ARGV.search(line), line)


class InstallFailureTailIsScrubbedTests(unittest.TestCase):
    """When the health check gives up, install.sh shows the last app log
    lines — and when the console runs it, that output lands in a file other
    host accounts can read. The access log carries every one-time link
    (reset, invite, verification) opened meanwhile, so the tail must come
    from the scrubbed snapshot, never the raw log."""

    def test_the_failure_path_never_tails_the_raw_log(self):
        src = (ROOT / "install.sh").read_text()
        tail = src[src.index("Still not responding after 5 minutes"):]
        self.assertNotRegex(tail, r"\$COMPOSE logs")
        self.assertIn('OIKONOME_ASSUME_YES=1 "$(cd .. && pwd)/oikonome.sh" logs',
                      tail)

    def test_the_snapshot_it_uses_redacts_a_token_link(self):
        t = ls.tempdir(self)
        inst = ls.stage(t, "COMPOSE_PROJECT_NAME=acme\n")
        binp = ls.make_bin(t)
        # the stand-in compose prints one access-log line for `logs`
        (binp / "docker").write_text(
            '#!/bin/sh\nif [ "$1 $2" = "compose logs" ]; then '
            'echo "GET /reset?token=FAKERESETTOKEN0001 HTTP/1.1"; fi\nexit 0\n')
        r = ls.bash(f'OIKONOME_ASSUME_YES=1 bash "{inst}/oikonome.sh" logs',
                    env=ls.run_env(binp, t / "log"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("/reset?token=<redacted>", r.stdout)
        self.assertNotIn("FAKERESETTOKEN0001", r.stdout)


if __name__ == "__main__":
    unittest.main()
