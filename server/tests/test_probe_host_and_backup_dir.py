"""Readiness probes follow the bind address; backups live in one folder.

With OIKONOME_BIND_ADDRESS pinned to one host address the app port is
published there only, so a probe of `localhost` reads a healthy app as down:
install fails, status says down, and a restore rolls itself back. Every
probe therefore derives its host from the bind setting.

OIKONOME_BACKUP_DIR is the one folder `./oikonome.sh backup`/`restore`,
the console's Restore lookup and the console's backup card (compose's
/state/backups bind) all use, so a scheduled dump aimed there is seen.
Unset, every one of them keeps the install's own backups/ folder.
"""

import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

APP = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = APP / "oikonome.sh"


def _bash(snippet: str, root: pathlib.Path, env: dict | None = None) -> str:
    """Run SNIPPET with the shared env helpers plus oikonome.sh's own
    app_host/backup_dir loaded, against the install at ROOT."""
    prelude = f"""
set -eu
. '{APP}/scripts/env-file.sh'
ROOT='{root}'
env_get() {{ dotenv_get "$ROOT/docker/.env" "$1"; }}
eval "$(sed -n '/^app_host() {{/,/^}}/p;/^backup_dir() {{/,/^}}/p;/^backup_dir_open() {{/,/^}}/p;/^owner_uid() /p' '{SCRIPT}')"
"""
    base = {k: v for k, v in os.environ.items()
            if k not in ("OIKONOME_BIND_ADDRESS", "OIKONOME_BACKUP_DIR")}
    r = subprocess.run(["bash", "-c", prelude + snippet], text=True,
                       capture_output=True, env={**base, **(env or {})},
                       timeout=30)
    if r.returncode:
        raise AssertionError(r.stderr)
    return r.stdout.strip()


class ProbeHostTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._td.name).resolve()
        (self.root / "docker").mkdir()

    def tearDown(self):
        self._td.cleanup()

    def test_wildcard_and_unset_binds_probe_localhost(self):
        for addr in ("", "0.0.0.0", "::", "[::]"):
            self.assertEqual(_bash(f"probe_host '{addr}'", self.root),
                             "localhost", addr)

    def test_a_pinned_address_is_probed_where_it_is_bound(self):
        self.assertEqual(_bash("probe_host 10.0.0.5", self.root), "10.0.0.5")
        self.assertEqual(_bash("probe_host 127.0.0.1", self.root),
                         "127.0.0.1")
        self.assertEqual(_bash("probe_host ::1", self.root), "[::1]")
        self.assertEqual(_bash("probe_host '[fd00::2]'", self.root),
                         "[fd00::2]")

    def test_app_host_reads_env_then_dotenv_like_compose(self):
        self.assertEqual(_bash("app_host", self.root), "localhost")
        (self.root / "docker/.env").write_text(
            'OIKONOME_BIND_ADDRESS="192.168.1.9"\n')
        self.assertEqual(_bash("app_host", self.root), "192.168.1.9")
        self.assertEqual(
            _bash("app_host", self.root, {"OIKONOME_BIND_ADDRESS": "::1"}),
            "[::1]")

    def test_no_probe_is_hardwired_to_localhost(self):
        for f in ("oikonome.sh", "install.sh"):
            self.assertNotRegex((APP / f).read_text(),
                                r'curl[^\n]*http://localhost', f)


class BackupDirTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._td.name).resolve()
        (self.root / "docker").mkdir()

    def tearDown(self):
        self._td.cleanup()

    def test_unset_keeps_the_install_backups_folder(self):
        self.assertEqual(_bash("backup_dir", self.root),
                         f"{self.root}/backups")

    def test_the_setting_moves_it_absolute_or_relative_to_docker(self):
        (self.root / "docker/.env").write_text(
            "OIKONOME_BACKUP_DIR=/srv/oik-dumps  # nightly\n")
        self.assertEqual(_bash("backup_dir", self.root), "/srv/oik-dumps")
        (self.root / "docker/.env").write_text(
            "OIKONOME_BACKUP_DIR=../../shared-dumps\n")
        self.assertEqual(_bash("backup_dir", self.root),
                         f"{self.root.parent}/shared-dumps")
        # the environment wins, as it does for compose
        self.assertEqual(
            _bash("backup_dir", self.root, {"OIKONOME_BACKUP_DIR": "/x/y"}),
            "/x/y")

    def test_compose_binds_the_same_folder_with_the_old_default(self):
        compose = (APP / "docker/compose.yaml").read_text()
        self.assertIn(
            "- ${OIKONOME_BACKUP_DIR:-../backups}:/state/backups:ro,z",
            compose)
        self.assertNotIn("- ../backups:/state/backups", compose)
        self.assertRegex((APP / "docker/.env.example").read_text(),
                         r"(?m)^#\s*OIKONOME_BACKUP_DIR=/")

    def test_the_console_restore_lookup_uses_the_shared_resolver(self):
        watcher = (APP / "scripts/install-host-command-watcher.sh").read_text()
        self.assertIn("backup_dir_resolve", watcher)
        self.assertNotRegex(watcher, r'restore_path="\\\$ROOT/backups/')


class ConsoleBackupCardTests(unittest.TestCase):
    def _label(self, value):
        from oikonome.web import adminconsole
        old = os.environ.pop("OIKONOME_BACKUP_DIR", None)
        try:
            if value is not None:
                os.environ["OIKONOME_BACKUP_DIR"] = value
            return adminconsole.backup_dir_label()
        finally:
            os.environ.pop("OIKONOME_BACKUP_DIR", None)
            if old is not None:
                os.environ["OIKONOME_BACKUP_DIR"] = old

    def test_card_names_the_folder_it_watches(self):
        self.assertEqual(self._label("/srv/oik-dumps"), "/srv/oik-dumps")
        self.assertEqual(self._label(None), "backups/ in the install folder")
        self.assertEqual(self._label(""), "backups/ in the install folder")

    def test_host_state_carries_the_label(self):
        from oikonome.web import adminconsole
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("OIKONOME_STATE_DIR")
            os.environ["OIKONOME_STATE_DIR"] = td
            try:
                state = adminconsole._host_state()
            finally:
                if old is None:
                    os.environ.pop("OIKONOME_STATE_DIR", None)
                else:
                    os.environ["OIKONOME_STATE_DIR"] = old
        self.assertIn("backup_dir", state)
        self.assertTrue(re.search(r"backups", state["backup_dir"]))


def _mode(p: pathlib.Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


class MovedBackupFolderIsListableTests(unittest.TestCase):
    """The console's backup card lists the moved folder from inside a
    container running under another uid: a 0700 folder reads there as no
    backups at all. The folder opens to go=rx; the dumps stay 0600."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._td.name).resolve()
        (self.root / "docker").mkdir()

    def tearDown(self):
        self._td.cleanup()

    def test_backup_sh_creates_its_destination_listable(self):
        # no container engine: the dump fails right after the folder is made
        fake = self.root / "fakebin"
        fake.mkdir()
        for tool in ("docker", "podman", "podman-compose"):
            (fake / tool).write_text("#!/bin/sh\nexit 1\n")
            (fake / tool).chmod(0o755)
        dest = self.root / "dumps" / "nightly"
        env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}",
               "OIKONOME_MAINT_LOCK_WAIT": "5"}
        r = subprocess.run(["bash", str(APP / "scripts/backup.sh"),
                            str(self.root), str(dest)], env=env, text=True,
                           capture_output=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(_mode(dest), 0o755)
        self.assertEqual(list(dest.iterdir()), [])  # the partial is gone

    def test_a_0700_moved_folder_is_opened_when_prepared(self):
        dest = self.root / "dumps"
        dest.mkdir(mode=0o700)
        dest.chmod(0o700)
        (self.root / "docker/.env").write_text(f"OIKONOME_BACKUP_DIR={dest}\n")
        _bash("backup_dir_open", self.root)
        self.assertEqual(_mode(dest), 0o755)

    def test_the_default_folder_is_left_as_it_is(self):
        dflt = self.root / "backups"
        dflt.mkdir(mode=0o700)
        dflt.chmod(0o700)
        _bash("backup_dir_open", self.root)
        self.assertEqual(_mode(dflt), 0o700)

    def test_every_preparation_opens_it(self):
        src = SCRIPT.read_text()
        for anchor in ('"$ROOT/scripts/backup.sh" "$ROOT" "$dest"',
                       'Snapshotting the database before the upgrade',
                       'pre="$dest/oikonome-pre-restore-',
                       'ok "Master key saved: $saved"'):
            i = src.index(anchor)
            self.assertIn("backup_dir_open", src[i - 600:i + 900], anchor)
        self.assertIn("chmod u=rwx,go=rx \"$BACKUP_DIR\"",
                      (APP / "install.sh").read_text())


class _StateDir:
    def __init__(self, env: dict):
        self.env = env

    def __enter__(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._td.name)
        self._old = {k: os.environ.get(k) for k in
                     ("OIKONOME_STATE_DIR", "OIKONOME_BACKUP_DIR")}
        os.environ["OIKONOME_STATE_DIR"] = str(self.path)
        os.environ.pop("OIKONOME_BACKUP_DIR", None)
        os.environ.update(self.env)
        return self.path

    def __exit__(self, *exc):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for p in self.path.rglob("*"):
            if p.is_dir():
                p.chmod(0o755)
        self._td.cleanup()


class ConsoleUnreadableBackupFolderTests(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, "root reads every folder")
    def test_an_unreadable_folder_is_not_reported_as_empty(self):
        from oikonome.web import adminconsole
        with _StateDir({"OIKONOME_BACKUP_DIR": "/srv/dumps"}) as st:
            b = st / "backups"
            b.mkdir()
            (b / "oikonome-20260101-000000.sql.gz").write_bytes(b"x")
            b.chmod(0o300)
            state = adminconsole._host_state()
        self.assertTrue(state["backups_available"])
        self.assertFalse(state["backups_readable"])
        self.assertEqual(state["backups"], [])
        card = (APP / "server/oikonome/web/templates/admin.html").read_text()
        self.assertIn("folder not readable by the app", card)

    def test_a_readable_folder_lists_its_dumps(self):
        from oikonome.web import adminconsole
        with _StateDir({}) as st:
            b = st / "backups"
            b.mkdir()
            (b / "oikonome-20260101-000000.sql.gz").write_bytes(b"x")
            state = adminconsole._host_state()
        self.assertTrue(state["backups_readable"])
        self.assertEqual(len(state["backups"]), 1)


class CommandWatcherRevisionTests(unittest.TestCase):
    """The root watcher is a frozen copy only its installer refreshes, so a
    console relying on what a newer checker does must say when the host's
    copy is older, and how to refresh it."""

    def _host(self, st: pathlib.Path, marker: str | None):
        (st / "cmd").mkdir()
        (st / "ops" / "watchers").mkdir(parents=True)
        if marker is not None:
            (st / "ops" / "watchers" / "command").write_text(marker)

    def test_revision_read_from_the_marker(self):
        from oikonome.web import adminconsole
        for marker, want in (("2026-10-01T00:00:00Z\n", 1),
                             ("2026-10-01T00:00:00Z\nrevision=2\n", 2),
                             ("2026-10-01T00:00:00Z\nrevision=junk\n", 1),
                             ("revision=7\n", 7)):
            with _StateDir({}) as st:
                self._host(st, marker)
                self.assertEqual(adminconsole.command_watcher_revision(),
                                 want, marker)
        with _StateDir({}) as st:
            self.assertEqual(adminconsole.command_watcher_revision(), 1)

    def test_an_old_watcher_is_flagged_only_for_a_moved_folder(self):
        from oikonome.web import adminconsole
        old = "2026-10-01T00:00:00Z\n"
        with _StateDir({"OIKONOME_BACKUP_DIR": "/srv/dumps"}) as st:
            self._host(st, old)
            msg = adminconsole.command_watcher_behind()
        self.assertIn("sudo sh scripts/install-host-command-watcher.sh", msg)
        self.assertIn("/srv/dumps", msg)
        with _StateDir({}) as st:  # default folder: every revision works
            self._host(st, old)
            self.assertEqual(adminconsole.command_watcher_behind(), "")
        with _StateDir({"OIKONOME_BACKUP_DIR": "/srv/dumps"}) as st:
            self._host(st, old + "revision=2\n")
            self.assertEqual(adminconsole.command_watcher_behind(), "")
        with _StateDir({"OIKONOME_BACKUP_DIR": "/srv/dumps"}) as st:
            # no watcher at all is a different notice, not this one
            self.assertEqual(adminconsole.command_watcher_behind(), "")

    def test_the_installer_writes_the_revision_the_console_expects(self):
        from oikonome.web import adminconsole
        src = (APP / "scripts/install-host-command-watcher.sh").read_text()
        m = re.search(r'echo "revision=(\d+)" >> "\$ROOT/ops-state/'
                      r'watchers/command"', src)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)),
                         adminconsole.COMMAND_WATCHER_REVISION)
        self.assertLess(src.index("watcher_mark \"$ROOT\" command"),
                        m.start())

    def test_the_sudo_line_is_documented_where_the_setting_is(self):
        line = "sudo sh scripts/install-host-command-watcher.sh <install-dir>"
        self.assertIn(line, (APP / "docker/.env.example").read_text())
        self.assertIn(line, (APP / "docs/quickstart.md").read_text())


if __name__ == "__main__":
    unittest.main()
