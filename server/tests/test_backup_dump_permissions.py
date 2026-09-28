"""A database dump is the whole database in plaintext SQL — every email
address, the entire transaction ledger, phone numbers, entity details. Only
the aggregator tokens are encrypted at rest; nothing else is. So every dump
the product writes must be owner-only, on a host whose default umask is
not, and older dumps sitting in the destination must be pulled down to the
same footing rather than left readable forever.

Two scripts write one: scripts/backup.sh (the nightly) and oikonome.sh
itself (the automatic pre-restore snapshot, which is the operator's only
rollback point and so is written on a database that is very much alive).
Owner-only has to hold from the file's FIRST BYTE, not from a chmod after
the pipeline: a dump of a large ledger takes minutes to produce, and a
chmod at the end leaves every one of those minutes open to any other local
account. The restore case below therefore watches the file WHILE pg_dump is
still streaming into it, which is the window a post-hoc chmod cannot close.

Nothing else in the suite runs either script, so a later edit that reorders
the umask past the gzip redirect, drops it, or breaks the chmod would go
out on every future backup with the suite still green. These shell the real
scripts out against a scratch destination with a stub `docker` in front of
them, the way the installer's own shell behaviour is tested
(test_installer_env_flag_zero_is_off.py).
"""

import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "backup.sh"
MANAGER = ROOT / "oikonome.sh"

# A stand-in pg_dump: real enough for the script's own sanity checks —
# it wants schema in the stream, and more than 1KB after gzip, so the
# filler is base64 of random bytes rather than something compressible.
_FAKE_DOCKER = """#!/bin/sh
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "exec" ]; then
  echo "CREATE TABLE transactions (id text);"
  head -c 40000 /dev/urandom | base64
  exit 0
fi
exit 1
"""


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


class BackupDumpPermissionTests(unittest.TestCase):
    def _run(self, umask: str = "022", seed_old: bool = False):
        """Run backup.sh under a permissive caller umask (the thing the
        script's own `umask 077` has to override) and return the
        destination directory."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "install" / "docker").mkdir(parents=True)
        (root / "install" / "docker" / ".env").write_text("OIKONOME_X=1\n")
        binp = root / "bin"
        binp.mkdir()
        (binp / "docker").write_text(_FAKE_DOCKER)
        (binp / "docker").chmod(0o755)
        dest = root / "backups"
        if seed_old:
            # a dump written before the permission fix existed
            dest.mkdir(mode=0o755)
            old = dest / "oikonome-20200101-000000.sql.gz"
            old.write_bytes(b"stale")
            old.chmod(0o644)
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}")
        r = subprocess.run(
            ["bash", "-c", f'umask {umask}; exec bash "$1" "$2" "$3"',
             "_", str(SCRIPT), str(root / "install"), str(dest)],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return dest

    def test_a_fresh_dump_and_its_directory_are_owner_only(self):
        dest = self._run()
        self.assertEqual(_mode(dest), 0o700,
                         "the backup directory the script created is "
                         "readable by other accounts on the host")
        dumps = sorted(dest.glob("oikonome-*.sql.gz"))
        self.assertEqual(len(dumps), 1)
        self.assertEqual(_mode(dumps[0]), 0o600,
                         "the database dump is readable beyond its owner")

    def test_a_dump_left_behind_by_an_older_backup_is_tightened(self):
        dest = self._run(seed_old=True)
        old = dest / "oikonome-20200101-000000.sql.gz"
        self.assertEqual(_mode(old), 0o600,
                         "a dump written before the permission fix stays "
                         "world-readable forever")
        # a directory the operator made themselves is their call to share
        self.assertEqual(_mode(dest), 0o755)


# The manager only ever shells out to `docker compose`, so one stub covers
# the whole restore run. pg_dump streams, pauses, then streams again, which
# gives the test a window to look at the half-written snapshot; the
# maintenance psql (`-d postgres`) refuses, which ends the run right after
# the snapshot instead of marching on into a health check that would sit
# there for a minute waiting for an app that does not exist.
_FAKE_ENGINE = """#!/bin/sh
[ "$1" = compose ] || exit 1
shift
case $1 in
  exec) shift; [ "$1" = "-T" ] && shift; shift ;;   # -T <service> <prog> …
  *) exit 0 ;;                                      # version / up / stop
esac
prog=$1; shift
case $prog in
  pg_isready) exit 0 ;;
  pg_dump)
    echo "CREATE TABLE transactions (id text);"
    head -c 40000 /dev/urandom | base64
    sleep "${FAKE_DUMP_SECONDS:-2}"
    head -c 40000 /dev/urandom | base64
    ;;
  psql)
    case " $* " in *" -d postgres "*) exit 1 ;; esac
    case " $* " in *" -c "*) : ;; *) cat >/dev/null ;; esac
    case " $* " in *"FROM users"*) echo 3 ;; esac
    ;;
esac
exit 0
"""


def _fake_dump(path: Path) -> None:
    """A file that passes dump_looks_real: real gzip, >1KB decompressed,
    carrying schema."""
    import gzip as _gzip
    import base64 as _b64
    path.write_bytes(_gzip.compress(
        b"CREATE TABLE transactions (id text);\n"
        + _b64.b64encode(os.urandom(4096))))


class RestoreSnapshotPermissionTests(unittest.TestCase):
    """The pre-restore snapshot oikonome.sh takes before it drops the
    database is a full plaintext dump of every tenant. It must be
    owner-only for its whole life, including while it is being written."""

    def _install(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        inst = root / "install"
        # symlinks, not copies: the run has to exercise the real manager
        # (SCRIPT_HOME is the *link's* directory, so ROOT lands here)
        (inst / "docker").mkdir(parents=True)
        (inst / "docker" / ".env").write_text(
            "COMPOSE_PROJECT_NAME=perm\nOIKONOME_PORT=8099\n")
        (inst / "oikonome.sh").symlink_to(MANAGER)
        (inst / "scripts").symlink_to(ROOT / "scripts")
        binp = root / "bin"
        binp.mkdir()
        for name in ("docker", "podman", "podman-compose"):
            p = binp / name
            p.write_text(_FAKE_ENGINE if name == "docker" else "#!/bin/sh\nexit 1\n")
            p.chmod(0o755)
        src = root / "incoming.sql.gz"
        _fake_dump(src)
        return inst, binp, src

    def test_the_pre_restore_snapshot_is_owner_only_while_it_is_written(self):
        inst, binp, src = self._install()
        env = dict(
            os.environ,
            PATH=f"{binp}:{os.environ['PATH']}",
            OIKONOME_ASSUME_YES="1",   # the typed confirm, not what's under test
            CI="1",
            FAKE_DUMP_SECONDS="2",
        )
        backups = inst / "backups"
        # a permissive caller umask — the thing the script's own umask has
        # to override. `setsid`, so /dev/tty cannot be reached and no prompt
        # can steal the developer's terminal.
        proc = subprocess.Popen(
            ["bash", "-c",
             'umask 022; exec bash "$1" restore "$2"',
             "_", str(inst / "oikonome.sh"), str(src)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=env, start_new_session=True)
        self.addCleanup(proc.kill)

        # Watch the snapshot appear and sample its mode while pg_dump is
        # still streaming. A post-hoc chmod cannot pass this.
        seen, deadline = [], time.monotonic() + 60
        while time.monotonic() < deadline:
            snaps = list(backups.glob("oikonome-pre-restore-*.sql.gz")) \
                if backups.is_dir() else []
            if snaps:
                try:
                    seen.append(_mode(snaps[0]))
                except FileNotFoundError:
                    pass
                if len(seen) >= 3:
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        out = proc.communicate(timeout=60)[0]

        self.assertTrue(seen, f"no pre-restore snapshot was ever written:\n{out}")
        self.assertEqual(
            set(seen), {0o600},
            "the pre-restore snapshot — a full plaintext dump of every "
            "tenant — was readable by other local accounts while it was "
            f"being written (modes seen: {[oct(m) for m in seen]})")
        snap = next(iter(backups.glob("oikonome-pre-restore-*.sql.gz")))
        self.assertEqual(_mode(snap), 0o600,
                         "the finished snapshot is readable beyond its owner")

    def test_the_backups_directory_stays_reachable_by_the_app_container(self):
        """Unlike backup.sh's external destination, ./backups is bind-mounted
        read-only into the app container (../backups:/state/backups) for the
        admin console's backup card, and the container's uid 10001 cannot
        traverse an 0700 directory owned by the host user. The dumps inside
        are 0600, which is the protection that matters."""
        inst, binp, src = self._install()
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}",
                   OIKONOME_ASSUME_YES="1", CI="1", FAKE_DUMP_SECONDS="0")
        subprocess.run(
            ["bash", "-c", 'umask 022; exec bash "$1" restore "$2"',
             "_", str(inst / "oikonome.sh"), str(src)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            env=env, start_new_session=True, timeout=120)
        dest = inst / "backups"
        self.assertTrue(dest.is_dir(), "the backup directory was never created")
        self.assertEqual(_mode(dest), 0o755)


if __name__ == "__main__":
    unittest.main()
