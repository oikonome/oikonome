"""`./oikonome.sh` warns when the engine is rootless Podman.

Under rootless Podman every client reaches the app from the pod network, so
auto-ban, per-IP limits and OIKONOME_ADMIN_IPS act on one shared address and
never on the real client — and with HTTPS on, the app port stays open to the
LAN from inside the trusted-proxy range. The script does not change the
networking, so it must say so wherever an operator sets up or inspects an
instance (install, https, status), naming the remedies — and stay quiet
under a rootful engine.

The real script runs from a scratch install folder with the prereq check,
install.sh and the `podman` binary stubbed, so nothing touches a container.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(args: list[str], *, rootless: bool, env_text: str = "") -> str:
    with tempfile.TemporaryDirectory() as d:
        inst = Path(d) / "inst"
        (inst / "docker").mkdir(parents=True)
        (inst / "scripts").mkdir()
        shutil.copy(ROOT / "oikonome.sh", inst / "oikonome.sh")
        shutil.copy(ROOT / "scripts" / "version-stamp.sh",
                    inst / "scripts" / "version-stamp.sh")
        shutil.copy(ROOT / "scripts" / "env-file.sh",
                    inst / "scripts" / "env-file.sh")
        # install takes the maintenance lock an upgrade shares with
        # backup/restore/reset
        shutil.copy(ROOT / "scripts" / "maint-lock.sh",
                    inst / "scripts" / "maint-lock.sh")
        (inst / "scripts" / "check-prereqs.sh").write_text(
            "require_prereqs() { COMPOSE=true; ENGINE=podman; }\n"
            "port_busy() { return 1; }\n")
        (inst / "install.sh").write_text("#!/bin/sh\nexit 0\n")
        (inst / "install.sh").chmod(0o755)
        # port 1: nothing listens, so the status health probe fails fast
        (inst / "docker" / ".env").write_text("OIKONOME_PORT=1\n" + env_text)
        bindir = Path(d) / "bin"
        bindir.mkdir()
        podman = bindir / "podman"
        podman.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = info ]; then echo %s; fi\n'
            "exit 0\n" % ("true" if rootless else "false"))
        podman.chmod(0o755)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                   HOME=str(Path(d)), OIKONOME_ASSUME_YES="1")
        r = subprocess.run(["bash", str(inst / "oikonome.sh"), *args],
                           capture_output=True, text=True, timeout=60,
                           env=env, stdin=subprocess.DEVNULL)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout + r.stderr


class RootlessPodmanWarningTests(unittest.TestCase):
    def assertWarns_(self, out: str):
        self.assertIn("Rootless Podman", out)
        self.assertIn("OIKONOME_ADMIN_IPS", out)
        self.assertIn("run the stack as root, or under Docker", out)
        self.assertIn("OIKONOME_TRUSTED_PROXIES", out)

    def test_install_warns_under_rootless_podman(self):
        self.assertWarns_(_run(["install"], rootless=True))

    def test_status_warns_under_rootless_podman(self):
        self.assertWarns_(_run(["status"], rootless=True))

    def test_https_warns_and_names_the_forgeable_headers(self):
        out = _run(["https", "fin.example.test"], rootless=True)
        self.assertWarns_(out)
        self.assertIn("forge X-Forwarded-For", out)

    def test_rootful_podman_stays_quiet(self):
        for args in (["install"], ["status"], ["https", "fin.example.test"]):
            self.assertNotIn("Rootless Podman",
                             _run(args, rootless=False), args)



@unittest.skipIf(os.geteuid() == 0, "as root the unit goes to /etc/systemd")
class PodmanStartsAtBootTests(unittest.TestCase):
    """Podman has no daemon to apply `restart: unless-stopped` at boot, so a
    rebooted Podman host leaves the stack down. `start-at-boot` installs the
    systemd user unit that brings it up, and turns lingering on so it runs
    without a login; under Docker there is nothing to install."""

    def _run(self, d: str, args: list[str], engine: str):
        inst = Path(d) / "inst"
        (inst / "docker").mkdir(parents=True)
        (inst / "scripts").mkdir()
        shutil.copy(ROOT / "oikonome.sh", inst / "oikonome.sh")
        shutil.copy(ROOT / "scripts" / "version-stamp.sh",
                    inst / "scripts" / "version-stamp.sh")
        shutil.copy(ROOT / "scripts" / "env-file.sh",
                    inst / "scripts" / "env-file.sh")
        compose = "podman compose" if engine == "podman" else "docker compose"
        (inst / "scripts" / "check-prereqs.sh").write_text(
            f'require_prereqs() {{ COMPOSE="{compose}"; ENGINE={engine}; }}\n'
            "port_busy() { return 1; }\n")
        (inst / "docker" / ".env").write_text("COMPOSE_PROJECT_NAME=acme\n")
        bindir = Path(d) / "bin"
        bindir.mkdir(exist_ok=True)
        log = Path(d) / "calls.log"
        for tool in ("podman", "docker", "systemctl", "loginctl"):
            (bindir / tool).write_text(
                f'#!/bin/sh\necho "{tool} $*" >> "{log}"\nexit 0\n')
            (bindir / tool).chmod(0o755)
        cfg = Path(d) / "cfg"
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                   HOME=str(Path(d)), XDG_CONFIG_HOME=str(cfg))
        r = subprocess.run(["bash", str(inst / "oikonome.sh"), *args],
                           capture_output=True, text=True, timeout=60,
                           env=env, stdin=subprocess.DEVNULL)
        unit = cfg / "systemd" / "user" / "oikonome-acme.service"
        calls = log.read_text() if log.exists() else ""
        return r, inst, unit, calls, bindir

    def test_podman_gets_a_boot_unit_and_lingering(self):
        with tempfile.TemporaryDirectory() as d:
            r, inst, unit, calls, bindir = self._run(
                d, ["start-at-boot"], "podman")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = unit.read_text()
            self.assertIn(f"ExecStart={bindir / 'podman'} compose up -d", text)
            self.assertIn(f"WorkingDirectory={inst / 'docker'}", text)
            self.assertIn("WantedBy=default.target", text)
            self.assertIn("systemctl --user enable oikonome-acme.service",
                          calls)
            self.assertIn("loginctl enable-linger", calls)

    def test_off_removes_the_unit(self):
        with tempfile.TemporaryDirectory() as d:
            self._run(d, ["start-at-boot"], "podman")
            shutil.rmtree(Path(d) / "inst")
            r, _inst, unit, calls, _b = self._run(
                d, ["start-at-boot", "off"], "podman")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(unit.exists())
            self.assertIn("systemctl --user disable oikonome-acme.service",
                          calls)

    def test_docker_needs_no_unit(self):
        with tempfile.TemporaryDirectory() as d:
            r, _inst, unit, calls, _b = self._run(
                d, ["start-at-boot"], "docker")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(unit.exists())
            self.assertNotIn("systemctl", calls)

    def test_the_installer_points_podman_hosts_at_it(self):
        src = (ROOT / "install.sh").read_text()
        i = src.index("start-at-boot")
        self.assertIn('"$ENGINE" = podman', src[i - 400:i])


if __name__ == "__main__":
    unittest.main()
