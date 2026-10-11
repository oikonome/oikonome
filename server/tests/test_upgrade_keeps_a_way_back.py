"""An upgrade keeps a way back, and never reports an upgrade it did not do.

Upgrading pulls new code and the app's start then runs its migrations,
several of which rewrite stored data for good; the image is rebuilt in
place under the same tag, so the only way back is the code's previous
version plus a copy of the database taken before the migrations ran. So:

- with a database that has accounts, the upgrade dumps it into ./backups
  first, and refuses to go on (nothing pulled, nothing installed) when that
  dump cannot be made;
- "new code" means code other than what last installed successfully, not
  "git still has commits to pull": a tree the operator pulled by hand, or
  main checked out again after a rollback, is just as new to the database,
  and its migrations run just the same;
- a checkout whose upstream was replaced by an unrelated history cannot be
  pulled, ever — the upgrade stops and says what to do, instead of quietly
  rebuilding the old version and reporting success;
- any other failed pull, run unattended (the admin console's host agent),
  fails the run rather than being recorded as done;
- the snapshot and the install run under the maintenance lock a backup,
  restore or reset holds (and the host's nightly reboot defers to), so the
  snapshot is never a half-loaded database and no reboot lands mid-upgrade;
  the snapshot is only given its listed name once it is complete.
"""

import gzip
import os
import subprocess
import unittest
from pathlib import Path

from . import lifecycle_stub as ls

ENV = "COMPOSE_PROJECT_NAME=acme\nOIKONOME_PORT=8999\n"


def _git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.dev",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.dev")
    r = subprocess.run(["git", *args], cwd=cwd, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class _Upgrade(unittest.TestCase):

    def setUp(self):
        self.t = ls.tempdir(self)
        # an install folder that is a git clone of an "upstream"
        self.inst = ls.stage(self.t, ENV)
        (self.inst / ".gitignore").write_text(
            "docker/.env\nbackups/\ninstall-ran\n")
        _git(self.inst, "init", "-q", "-b", "main")
        _git(self.inst, "add", "-A")
        _git(self.inst, "commit", "-q", "-m", "release one")
        self.up = self.t / "upstream.git"
        _git(self.t, "clone", "-q", "--bare", str(self.inst), str(self.up))
        _git(self.inst, "remote", "add", "origin", str(self.up))
        _git(self.inst, "fetch", "-q", "origin")
        _git(self.inst, "branch", "-q", "--set-upstream-to=origin/main")
        self.work = self.t / "publisher"
        _git(self.t, "clone", "-q", str(self.up), str(self.work))
        self.binp = ls.make_bin(self.t)
        self.log = self.t / "log"

    def _publish_next(self):
        (self.work / "NEXT").write_text("release two\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "release two")
        _git(self.work, "push", "-q", "origin", "main")

    def _upgrade(self, **knobs):
        return ls.bash(f'bash "{self.inst}/oikonome.sh" upgrade </dev/null',
                       env=ls.run_env(self.binp, self.log, **knobs))

    def _dump(self, body: str) -> Path:
        p = self.t / "dump.sql"
        p.write_text("-- dump\n" + ("CREATE TABLE t (x int);\n" * 80) + body)
        return p

    def _record_installed(self, sha: str) -> None:
        # what install.sh writes once an install has come up healthy
        envf = self.inst / "docker" / ".env"
        envf.write_text(envf.read_text() + f"OIKONOME_INSTALLED_COMMIT={sha}\n")

    def _snapshots(self):
        return sorted((self.inst / "backups").glob("oikonome-pre-upgrade-*.sql.gz"))


class UpgradeSnapshotTests(_Upgrade):

    def test_a_database_with_accounts_is_dumped_before_new_code_lands(self):
        self._publish_next()
        r = self._upgrade(STUB_USERS=3, STUB_DUMP_FILE=self._dump("LIVE\n"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 1, r.stdout)
        self.assertIn(b"LIVE", gzip.decompress(snaps[0].read_bytes()))
        self.assertEqual(snaps[0].stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.inst / "NEXT").exists())
        self.assertTrue((self.inst / "install-ran").exists())
        self.assertIn(snaps[0].name, r.stdout)

    def test_no_snapshot_with_accounts_means_no_upgrade(self):
        self._publish_next()
        head = _git(self.inst, "rev-parse", "HEAD")
        r = self._upgrade(STUB_USERS=3, STUB_DUMP_RC=1)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(_git(self.inst, "rev-parse", "HEAD"), head)
        self.assertFalse((self.inst / "install-ran").exists())
        self.assertEqual(self._snapshots(), [])

    def test_code_already_installed_takes_no_snapshot(self):
        self._record_installed(_git(self.inst, "rev-parse", "HEAD"))
        r = self._upgrade(STUB_USERS=3, STUB_DUMP_FILE=self._dump("LIVE\n"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._snapshots(), [])
        self.assertTrue((self.inst / "install-ran").exists())

    def test_code_pulled_by_hand_is_still_snapshotted(self):
        self._record_installed(_git(self.inst, "rev-parse", "HEAD"))
        self._publish_next()
        _git(self.inst, "pull", "-q", "--ff-only")
        r = self._upgrade(STUB_USERS=3, STUB_DUMP_FILE=self._dump("LIVE\n"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 1, r.stdout)
        self.assertIn(b"LIVE", gzip.decompress(snaps[0].read_bytes()))
        self.assertIn("To undo this upgrade", r.stdout)
        self.assertIn(snaps[0].name, r.stdout)

    def test_returning_to_main_after_a_rollback_is_snapshotted(self):
        # main already holds release two; the old code was installed by
        # hand to roll back, so release one is what migrated the database
        old = _git(self.inst, "rev-parse", "HEAD")
        self._publish_next()
        _git(self.inst, "pull", "-q", "--ff-only")
        self._record_installed(old)
        r = self._upgrade(STUB_USERS=3, STUB_DUMP_FILE=self._dump("LIVE\n"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self._snapshots()), 1, r.stdout)

    def test_no_record_of_the_installed_code_is_snapshotted(self):
        # an instance from before the record, or whose last install failed
        r = self._upgrade(STUB_USERS=3, STUB_DUMP_FILE=self._dump("LIVE\n"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self._snapshots()), 1, r.stdout)

    def test_a_manual_pull_that_cannot_be_snapshotted_installs_nothing(self):
        self._record_installed(_git(self.inst, "rev-parse", "HEAD"))
        self._publish_next()
        _git(self.inst, "pull", "-q", "--ff-only")
        r = self._upgrade(STUB_USERS=3, STUB_DUMP_RC=1)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertFalse((self.inst / "install-ran").exists())


class InstallRecordsWhatItInstalledTests(unittest.TestCase):
    """install.sh writes down the commit it installed, and only once the
    stack has come up healthy, so the next upgrade can tell whether the
    database was last migrated by the code about to be installed. An
    install that failed records nothing, and the next attempt snapshots."""

    def _stage(self, up_rc: int):
        from .test_reset_and_uninstall_keep_the_master_key import (
            KEY, _stage_installer)
        t = ls.tempdir(self)
        inst, binp = _stage_installer(
            t, env_text=f"COMPOSE_PROJECT_NAME=acme\nOIKONOME_MASTER_KEY={KEY}\n")
        # the Postgres major check passes; compose up answers up_rc; the
        # app answers its readiness probe at once
        (inst / "scripts" / "pg-major-migrate.sh").write_text(
            "#!/bin/sh\nexit 0\n")
        (binp / "docker").write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = compose ] && [ "$2" = up ]; then exit {up_rc}; fi\n'
            "exit 0\n")
        (binp / "curl").write_text("#!/bin/sh\nexit 0\n")
        (binp / "curl").chmod(0o755)
        (inst / ".gitignore").write_text("docker/.env\n")
        _git(inst, "init", "-q", "-b", "main")
        _git(inst, "add", "-A")
        _git(inst, "commit", "-q", "-m", "release one")
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}")
        for k in ("OIKONOME_KEEP_MASTER_KEY", "OIKONOME_RESET_SETTINGS",
                  "OIKONOME_PORT"):
            env.pop(k, None)
        env["OIKONOME_ASSUME_YES"] = "1"
        r = subprocess.run(["bash", str(inst / "install.sh")],
                           capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=60)
        recorded = [ln.split("=", 1)[1] for ln in
                    (inst / "docker" / ".env").read_text().splitlines()
                    if ln.startswith("OIKONOME_INSTALLED_COMMIT=")]
        return r, recorded, _git(inst, "rev-parse", "HEAD")

    def test_a_healthy_install_records_its_commit(self):
        r, recorded, head = self._stage(up_rc=0)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(recorded, [head])

    def test_a_failed_install_records_nothing(self):
        r, recorded, _head = self._stage(up_rc=1)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(recorded, [])


class UpgradeHoldsTheMaintenanceLockTests(_Upgrade):

    def _probe_install(self):
        # install.sh records whether the lock is held while it runs, and
        # that it was told so (so pg-major-migrate does not wait on it)
        (self.inst / "install.sh").write_text(
            "#!/bin/bash\n"
            'd=$(dirname "$0")\n'
            'if flock -n "$d/.maintenance.lock" true; then s=free; else s=held; fi\n'
            'echo "$s ${OIKONOME_MAINT_LOCK_HELD:+told}" > "$d/install-ran"\n')
        _git(self.inst, "commit", "-qam", "probe")
        _git(self.inst, "push", "-q", "origin", "HEAD:main")
        _git(self.work, "pull", "-q")

    def test_install_runs_with_the_lock_held(self):
        self._probe_install()
        self._publish_next()
        r = self._upgrade(STUB_USERS=3, STUB_DUMP_FILE=self._dump("LIVE\n"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.inst / "install-ran").read_text().split(),
                         ["held", "told"])

    def test_an_upgrade_waits_for_a_running_restore(self):
        self._publish_next()
        lock = self.inst / ".maintenance.lock"
        holder = subprocess.Popen(
            ["flock", str(lock), "sh", "-c", "read x"],
            stdin=subprocess.PIPE)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.stdin.close)
        # the holder has the lock once a non-blocking take fails
        for _ in range(200):
            if subprocess.run(["flock", "-n", str(lock), "true"]).returncode:
                break
        head = _git(self.inst, "rev-parse", "HEAD")
        r = self._upgrade(STUB_USERS=3, STUB_DUMP_FILE=self._dump("LIVE\n"),
                          OIKONOME_MAINT_LOCK_WAIT=0)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self._snapshots(), [])
        self.assertEqual(_git(self.inst, "rev-parse", "HEAD"), head)
        self.assertFalse((self.inst / "install-ran").exists())

    def test_a_failed_snapshot_leaves_nothing_under_a_listed_name(self):
        self._publish_next()
        r = self._upgrade(STUB_USERS=0, STUB_DUMP_RC=1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(list((self.inst / "backups").iterdir()), [])


class UpgradeNeverFakesSuccessTests(_Upgrade):

    def test_a_recreated_upstream_history_stops_with_the_way_out(self):
        # the publisher replaces the upstream with a single unrelated root
        _git(self.work, "checkout", "-q", "--orphan", "fresh")
        (self.work / "NEXT").write_text("recreated\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "recreated")
        _git(self.work, "push", "-q", "-f", "origin", "fresh:main")
        r = self._upgrade(OIKONOME_ASSUME_YES=1)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("no longer share a commit", r.stdout)
        self.assertIn("reset --hard", r.stdout)
        self.assertFalse((self.inst / "install-ran").exists())

    def test_an_unattended_upgrade_whose_pull_fails_fails(self):
        self._publish_next()
        # a local edit to a file the incoming release also creates
        (self.inst / "NEXT").write_text("local edit\n")
        _git(self.inst, "add", "NEXT")
        _git(self.inst, "commit", "-q", "-m", "local")
        r = self._upgrade(OIKONOME_ASSUME_YES=1)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertFalse((self.inst / "install-ran").exists())


if __name__ == "__main__":
    unittest.main()
