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


if __name__ == "__main__":
    unittest.main()
