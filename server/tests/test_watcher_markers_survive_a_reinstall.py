"""Re-running one host-watcher installer must not make the other look gone.

The container judges each console lever by a marker its installer leaves
(jobs/host_watchers.ready). A host whose two watchers were installed before
markers existed has no markers directory, and the first installer re-run
creates it holding only its own marker — from then on the other watcher,
still installed and running, reads as absent: the reboot lever says "not
configured" and the nightly auto-reboot quietly stops. The installers'
shared marker step asks systemd about the other watcher, and never removes
a marker.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from oikonome.jobs import host_watchers

REPO = pathlib.Path(__file__).resolve().parents[2]
HELPER = REPO / "scripts" / "watcher-marker.sh"


class WatcherMarkerTests(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = pathlib.Path(self.td.name) / "oikonome"
        self.bin = pathlib.Path(self.td.name) / "bin"
        self.bin.mkdir()
        (self.root / "state" / "cmd").mkdir(parents=True)

    def _systemctl(self, enabled: set[str]):
        """A stand-in systemctl: `is-enabled --quiet <unit>` succeeds only
        for the units named."""
        script = self.bin / "systemctl"
        lines = ["#!/bin/sh",
                 '[ "$1" = is-enabled ] || exit 1',
                 "for u; do :; done"]
        lines += [f'[ "$u" = "{u}" ] && exit 0' for u in sorted(enabled)]
        lines.append("exit 1")
        script.write_text("\n".join(lines) + "\n")
        script.chmod(0o755)

    def _mark(self, kind: str):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        subprocess.run(
            ["sh", "-c", f'. "{HELPER}"; watcher_mark "$0" {kind} oikonome',
             str(self.root)], check=True, env=env, capture_output=True)

    def _ready(self, kind: str) -> bool:
        # the container's view: /state is the install's state, with the
        # ops-state directory mounted at /state/ops
        state = pathlib.Path(self.td.name) / "view"
        if not state.exists():
            state.mkdir()
            (state / "cmd").symlink_to(self.root / "state" / "cmd")
            (state / "ops").symlink_to(self.root / "ops-state")
        with mock.patch.dict(os.environ, {"OIKONOME_STATE_DIR": str(state)}):
            return host_watchers.ready(kind)

    def test_reinstalling_the_command_watcher_keeps_the_reboot_lever(self):
        self._systemctl({"oikonome-reboot-watcher-oikonome.path",
                         "oikonome-command-watcher-oikonome.path"})
        self._mark("command")
        self.assertTrue(self._ready("command"))
        self.assertTrue(self._ready("reboot"),
                        "the still-installed reboot watcher reads as absent")

    def test_a_watcher_systemd_does_not_have_is_not_vouched_for(self):
        self._systemctl({"oikonome-command-watcher-oikonome.path"})
        self._mark("command")
        self.assertTrue(self._ready("command"))
        self.assertFalse(self._ready("reboot"))

    def test_an_older_shared_name_unit_counts(self):
        self._systemctl({"oikonome-command-watcher.path"})
        self._mark("reboot")
        self.assertTrue(self._ready("command"))

    def test_no_marker_is_ever_removed(self):
        self._systemctl(set())
        self._mark("reboot")
        self._mark("command")
        self.assertTrue(self._ready("reboot"))
        self.assertTrue(self._ready("command"))

    def test_the_markers_belong_to_the_install_owner(self):
        # The installers run as root and uninstall runs as the owner, who
        # cannot delete files inside a root-owned directory. A stand-in
        # chown records what the step hands over, and to whom.
        self._systemctl({"oikonome-reboot-watcher-oikonome.path"})
        log = pathlib.Path(self.td.name) / "chown.log"
        stub = self.bin / "chown"
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n')
        stub.chmod(0o755)
        self._mark("command")
        owner = self.root.owner()
        calls = log.read_text().splitlines() if log.exists() else []
        self.assertIn(f"-R {owner} {self.root}/ops-state/watchers", calls,
                      "the markers stay root-owned, so a non-root "
                      "uninstall cannot remove the install folder")
        self.assertIn(f"{owner} {self.root}/ops-state", calls)

    def test_both_installers_use_the_shared_step(self):
        for name in ("install-host-command-watcher.sh",
                     "install-host-reboot-watcher.sh"):
            src = (REPO / "scripts" / name).read_text()
            self.assertIn('watcher-marker.sh"', src, name)
            self.assertIn("watcher_mark \"$ROOT\"", src, name)


class AMarkerlessCommandWatcherKeepsItsTrueRevision(unittest.TestCase):
    """The reboot installer marks a command watcher it finds enabled with no
    marker. Such a checker was installed before markers existed, so unless
    the checker itself names a revision the marker must not claim one: a
    marker saying "revision 2" over an older checker hides the console's
    notice that a restore from a moved backup folder will fail."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        base = pathlib.Path(self.td.name)
        self.root = base / "oikonome"
        (self.root / "state" / "cmd").mkdir(parents=True)
        self.bin = base / "bin"
        self.bin.mkdir()
        (self.bin / "systemctl").write_text(
            "#!/bin/sh\nfor u; do :; done\n"
            '[ "$u" = oikonome-command-watcher-oikonome.path ] && exit 0\n'
            "exit 1\n")
        (self.bin / "systemctl").chmod(0o755)
        self.checker = base / "oikonome-command-run"
        self.view = base / "view"
        self.view.mkdir()
        (self.view / "cmd").symlink_to(self.root / "state" / "cmd")
        (self.view / "ops").symlink_to(self.root / "ops-state")

    def _reboot_install(self, checker_text: str | None):
        if checker_text is not None:
            self.checker.write_text(checker_text)
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                   WATCHER_COMMAND_RUN=str(self.checker))
        subprocess.run(
            ["sh", "-c", f'. "{HELPER}"; watcher_mark "$0" reboot oikonome',
             str(self.root)], check=True, env=env, capture_output=True)

    def _console(self):
        from oikonome.web import adminconsole
        env = {"OIKONOME_STATE_DIR": str(self.view),
               "OIKONOME_BACKUP_DIR": "/srv/dumps"}
        with mock.patch.dict(os.environ, env):
            return (adminconsole.command_watcher_revision(),
                    adminconsole.command_watcher_behind())

    def test_a_checker_that_names_no_revision_reads_as_behind(self):
        self._reboot_install("#!/bin/sh\nset -u\n")
        rev, behind = self._console()
        self.assertEqual(rev, 1)
        self.assertIn("install-host-command-watcher.sh", behind)

    def test_an_unreadable_checker_reads_as_behind(self):
        self._reboot_install(None)
        rev, behind = self._console()
        self.assertEqual(rev, 1)
        self.assertNotEqual(behind, "")

    def test_a_checker_that_names_its_revision_is_believed(self):
        self._reboot_install(
            "#!/bin/sh\n# oikonome-command-run revision=2\nset -u\n")
        self.assertEqual(self._console(), (2, ""))

    def test_the_generated_checker_names_the_revision_the_marker_does(self):
        import re
        from oikonome.web import adminconsole
        src = (REPO / "scripts/install-host-command-watcher.sh").read_text()
        body = src.split("cat > /usr/local/bin/oikonome-command-run "
                         "<<SCRIPT\n", 1)[1]
        m = re.match(r"#!/bin/sh\n# oikonome-command-run revision=(\d+)\n",
                     body)
        self.assertIsNotNone(m, "the checker does not name its revision")
        self.assertEqual(int(m.group(1)),
                         adminconsole.COMMAND_WATCHER_REVISION)
        marker = re.search(r'echo "revision=(\d+)" >> "\$ROOT/ops-state/'
                           r'watchers/command"', src)
        self.assertEqual(int(marker.group(1)), int(m.group(1)))


if __name__ == "__main__":
    unittest.main()
