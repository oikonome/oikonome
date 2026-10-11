"""The maintenance lock serialises backup against restore/reset/uninstall
without the backup deadlocking on itself.

flock is per open-file-description: `./oikonome.sh backup` taking the lock
and then running scripts/backup.sh — which re-opens the same file and takes
it again — makes every on-demand backup wait the whole timeout and "skip".
So only backup.sh takes it, and a held lock makes backup.sh refuse fast.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

APP = pathlib.Path(__file__).resolve().parents[2]


class BackupLockTests(unittest.TestCase):
    def test_cmd_backup_does_not_take_the_lock_backup_sh_takes(self):
        src = (APP / "oikonome.sh").read_text()
        body = src[src.index("cmd_backup() {"):src.index("}", src.index("cmd_backup() {"))]
        self.assertNotIn("maint_lock", body.replace("# NO maint_lock", ""))
        for fn in ("cmd_restore", "cmd_reset", "cmd_uninstall"):
            i = src.index(fn + "() {")
            self.assertIn("maint_lock", src[i:i + 600], fn)
        self.assertIn("flock -w", (APP / "scripts/backup.sh").read_text())

    def test_a_held_lock_makes_backup_sh_refuse_instead_of_racing(self):
        with tempfile.TemporaryDirectory() as td:
            install = pathlib.Path(td) / "inst"; install.mkdir()
            (install / "docker").mkdir()
            lock = install / ".maintenance.lock"
            holder = subprocess.Popen(
                ["bash", "-c", f"exec 9>'{lock}'; flock 9; sleep 30"])
            try:
                # wait until the holder really holds it
                for _ in range(50):
                    probe = subprocess.run(
                        ["bash", "-c", f"exec 9>'{lock}'; flock -n 9"],
                        capture_output=True)
                    if probe.returncode != 0:
                        break
                    import time; time.sleep(0.1)
                r = subprocess.run(
                    ["bash", str(APP / "scripts/backup.sh"), str(install),
                     str(pathlib.Path(td) / "dest")],
                    capture_output=True, text=True,
                    env={**os.environ, "OIKONOME_MAINT_LOCK_WAIT": "1"},
                    timeout=30)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("backup skipped", r.stderr)
            finally:
                holder.kill()


def _hold(lock: pathlib.Path):
    """A process holding the maintenance lock, returned once it holds it."""
    import time
    holder = subprocess.Popen(
        ["bash", "-c", f"exec 9>'{lock}'; flock 9; sleep 30"])
    for _ in range(50):
        probe = subprocess.run(
            ["bash", "-c", f"exec 9>'{lock}'; flock -n 9"],
            capture_output=True)
        if probe.returncode != 0:
            break
        time.sleep(0.1)
    return holder


class MajorMigrationTakesTheLockTests(unittest.TestCase):
    """The in-place Postgres major migration rewrites the whole database,
    so it shares the lock: a scheduled backup must not connect mid-reload
    (it would stop the reload's DROP DATABASE, or dump a half-loaded
    database as the newest good backup)."""

    def _stage(self, td: str):
        root = pathlib.Path(td)
        inst = root / "inst"
        (inst / "docker").mkdir(parents=True)
        (inst / "data" / "postgres").mkdir(parents=True)
        # an older major than the compose file pins: a migration is due
        (inst / "data" / "postgres" / "PG_VERSION").write_text("9\n")
        (inst / "docker" / "compose.yaml").write_text(
            (APP / "docker" / "compose.yaml").read_text())
        (inst / "docker" / ".env").write_text("POSTGRES_PASSWORD=pw\n")
        binp = root / "bin"
        binp.mkdir()
        log = root / "engine.log"
        log.write_text("")
        (binp / "docker").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
            f'echo "$*" >> "{log}"\nexit 0\n')
        (binp / "sudo").write_text(
            '#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n')
        for f in ("docker", "sudo"):
            (binp / f).chmod(0o755)
        env = {**os.environ, "PATH": f"{binp}:{os.environ['PATH']}",
               "OIKONOME_MAINT_LOCK_WAIT": "1"}
        env.pop("OIKONOME_MAINT_LOCK_HELD", None)
        return inst, env, log

    def test_a_held_lock_stops_the_migration_before_it_starts(self):
        with tempfile.TemporaryDirectory() as td:
            inst, env, log = self._stage(td)
            holder = _hold(inst / ".maintenance.lock")
            try:
                r = subprocess.run(
                    ["bash", str(APP / "scripts/pg-major-migrate.sh"),
                     str(inst)], capture_output=True, text=True, env=env,
                    timeout=60)
            finally:
                holder.kill()
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("still running", r.stderr)
            self.assertNotIn("Migrating", r.stdout)
            self.assertEqual(log.read_text(), "", "the migration touched "
                             "the stack while a backup held the lock")

    def test_a_caller_that_holds_the_lock_is_not_waited_on(self):
        with tempfile.TemporaryDirectory() as td:
            inst, env, _log = self._stage(td)
            env["OIKONOME_MAINT_LOCK_HELD"] = str(inst)
            holder = _hold(inst / ".maintenance.lock")
            try:
                r = subprocess.run(
                    ["bash", str(APP / "scripts/pg-major-migrate.sh"),
                     str(inst)], capture_output=True, text=True, env=env,
                    timeout=120)
            finally:
                holder.kill()
            self.assertNotIn("still running", r.stderr)
            self.assertIn("Migrating", r.stdout)

    def test_the_manager_tells_its_children_it_holds_the_lock(self):
        src = (APP / "oikonome.sh").read_text()
        body = src[src.index("maint_lock() {"):src.index("\n}", src.index("maint_lock() {"))]
        self.assertIn("OIKONOME_MAINT_LOCK_HELD", body)



def _source_manager(root: pathlib.Path, body: str):
    """Run `body` in a shell that has sourced oikonome.sh's functions (the
    `help` argument makes its dispatch print usage and fall through)
    with ROOT pointed at a scratch instance. No controlling terminal, so
    every prompt reads stdin."""
    script = (f"source ./oikonome.sh help >/dev/null\n"
              f"ROOT='{root}'\n" + body)
    env = {**os.environ, "OIKONOME_MAINT_LOCK_WAIT": "1"}
    env.pop("OIKONOME_MAINT_LOCK_HELD", None)
    return subprocess.run(["bash", "-c", script], cwd=APP, env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          start_new_session=True, timeout=60)


def _lock_free(lock: pathlib.Path) -> str:
    return (f"if bash -c \"exec 9>'{lock}'; flock -n 9\"; then echo FREE; "
            f"else echo HELD; fi\n")


class MenuReleasesTheLockTests(unittest.TestCase):
    """The menu is one long-lived shell. A restore or reset run from it must
    give the lock back when it ends — however it ends — or every later
    backup, scheduled or 'Back up now', waits out its timeout and skips."""

    def _instance(self, td):
        root = pathlib.Path(td) / "inst"
        (root / "backups").mkdir(parents=True)
        (root / "backups" / "oikonome-20260101-000000.sql.gz").write_bytes(b"")
        return root

    def test_menu_runs_lock_taking_commands_in_their_own_scope(self):
        src = (APP / "oikonome.sh").read_text()
        menu = src[src.index("menu() {"):src.index("\n}\n", src.index("menu() {"))]
        self.assertIn("5) in_lock_scope cmd_restore", menu)
        self.assertIn("6) in_lock_scope cmd_reset", menu)

    def test_a_cancelled_restore_releases_the_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._instance(td)
            r = _source_manager(root, "in_lock_scope cmd_restore || true\n"
                                + _lock_free(root / ".maintenance.lock"))
            self.assertIn("Cancelled", r.stdout, r.stderr)
            self.assertIn("FREE", r.stdout, r.stderr)

    def test_a_restore_that_dies_releases_the_lock_and_the_menu_lives(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._instance(td)
            # a non-number at the picker makes cmd_restore die
            r = _source_manager(
                root, "in_lock_scope cmd_restore <<<'x' || echo SURVIVED\n"
                + _lock_free(root / ".maintenance.lock"))
            self.assertIn("SURVIVED", r.stdout, r.stderr)
            self.assertIn("FREE", r.stdout, r.stderr)

    def test_the_old_direct_call_would_keep_it(self):
        # the control: the same cancel without the scope leaves the lock
        # held by the calling shell — what the menu used to do
        with tempfile.TemporaryDirectory() as td:
            root = self._instance(td)
            r = _source_manager(root, "cmd_restore || true\n"
                                + _lock_free(root / ".maintenance.lock"))
            self.assertIn("HELD", r.stdout, r.stderr)


class StaleLockIsNamedTests(unittest.TestCase):
    def _skip(self, holder_age_s: int, pid: int):
        with tempfile.TemporaryDirectory() as td:
            install = pathlib.Path(td) / "inst"; install.mkdir()
            (install / "docker").mkdir()
            import time
            (install / ".maintenance.lock.holder").write_text(
                f"{pid} {int(time.time()) - holder_age_s} restore\n")
            holder = _hold(install / ".maintenance.lock")
            try:
                return subprocess.run(
                    ["bash", str(APP / "scripts/backup.sh"), str(install),
                     str(pathlib.Path(td) / "dest")],
                    capture_output=True, text=True,
                    env={**os.environ, "OIKONOME_MAINT_LOCK_WAIT": "1",
                         "OIKONOME_MAINT_LOCK_STALE_HOURS": "6"},
                    timeout=30)
            finally:
                holder.kill()

    def test_a_lock_held_past_the_stale_age_is_reported_as_stale(self):
        r = self._skip(7 * 3600, 2 ** 22 + 7)   # a pid that is not running
        self.assertEqual(r.returncode, 1)
        self.assertIn("STALE", r.stderr)
        self.assertIn("'restore'", r.stderr)
        self.assertIn("7 hours ago", r.stderr)
        self.assertIn("no longer running", r.stderr)

    def test_a_fresh_lock_is_an_ordinary_skip(self):
        r = self._skip(60, os.getpid())
        self.assertEqual(r.returncode, 1)
        self.assertIn("backup skipped: a restore/reset is running", r.stderr)
        self.assertNotIn("STALE", r.stderr)


class HolderNamesTheRealHolderTests(unittest.TestCase):
    """The holder file is what the skip message tells the operator, so it
    must name the process holding the lock now: every taker writes it,
    each clears it when it lets go, and the lock is called stale only when
    the recorded process no longer holds it — never from its age alone."""

    def test_a_long_operation_still_holding_the_lock_is_not_stale(self):
        # a migration that has held the lock for a day is still running;
        # telling the operator to delete the lock under it would let a
        # backup connect mid-reload
        with tempfile.TemporaryDirectory() as td:
            install = pathlib.Path(td) / "inst"; install.mkdir()
            (install / "docker").mkdir()
            import time
            holder = _hold(install / ".maintenance.lock")
            try:
                (install / ".maintenance.lock.holder").write_text(
                    f"{holder.pid} {int(time.time()) - 24 * 3600} "
                    "pg-major-migrate\n")
                r = subprocess.run(
                    ["bash", str(APP / "scripts/backup.sh"), str(install),
                     str(pathlib.Path(td) / "dest")],
                    capture_output=True, text=True,
                    env={**os.environ, "OIKONOME_MAINT_LOCK_WAIT": "1",
                         "OIKONOME_MAINT_LOCK_STALE_HOURS": "6"},
                    timeout=30)
            finally:
                holder.kill()
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("STALE", r.stderr)
        self.assertIn("'pg-major-migrate'", r.stderr)
        self.assertIn("still running", r.stderr)

    def test_backup_clears_its_holder_when_it_ends(self):
        with tempfile.TemporaryDirectory() as td:
            install = pathlib.Path(td) / "inst"; install.mkdir()
            # no docker/ folder: the run fails right after taking the lock
            subprocess.run(
                ["bash", str(APP / "scripts/backup.sh"), str(install),
                 str(pathlib.Path(td) / "dest")],
                capture_output=True, text=True, timeout=30)
            self.assertFalse(
                (install / ".maintenance.lock.holder").exists(),
                "a finished backup left itself named as the holder")

    def test_every_taker_writes_the_holder(self):
        for rel in ("scripts/backup.sh", "scripts/pg-major-migrate.sh"):
            self.assertIn("maint_holder_write", (APP / rel).read_text(), rel)
        src = (APP / "oikonome.sh").read_text()
        body = src[src.index("maint_lock() {"):
                   src.index("\n}", src.index("maint_lock() {"))]
        self.assertIn("maint_holder_write", body)
        self.assertIn("maint_holder_clear", body)

    def test_the_manager_records_and_clears_the_shell_that_holds_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "inst"; root.mkdir()
            holder = root / ".maintenance.lock.holder"
            r = _source_manager(
                root,
                "( maint_lock restore; read -r p _ < '" + str(holder) + "'; "
                "[ \"$p\" = \"$BASHPID\" ] && echo SELF )\n"
                f"[ -e '{holder}' ] && echo LEFT || echo CLEARED\n")
            self.assertIn("SELF", r.stdout, r.stderr)
            self.assertIn("CLEARED", r.stdout, r.stderr)

    def test_bash_without_bashpid_takes_the_lock(self):
        """bash 3.2 (macOS) has no $BASHPID; under `set -u` naming it
        aborted restore, reset and uninstall right after the lock."""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "inst"; root.mkdir()
            holder = root / ".maintenance.lock.holder"
            r = _source_manager(
                root,
                "( unset BASHPID; set -u; maint_lock restore && echo TOOK; "
                f"read -r p _ < '{holder}'; echo \"PID=$p\" )\n")
            self.assertIn("TOOK", r.stdout, r.stderr)
            pid = r.stdout.split("PID=", 1)[1].split()[0]
            self.assertTrue(pid.isdigit(), r.stdout)
