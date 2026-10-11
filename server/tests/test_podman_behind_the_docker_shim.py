"""A `docker` that is really podman is treated as podman.

The podman-docker package installs a `docker` wrapper that answers
`docker compose` like Docker does, so detection used to call the engine
Docker. There is no Docker daemon behind it, though, and nothing applies
the stack's restart policy at boot: `start-at-boot` then said "nothing to
set up" and wrote no unit, the installer left out its reboot hint, and the
first host reboot left the instance down.

The real scripts run with `docker`, `podman`, `systemctl` and `loginctl`
stubbed, so nothing touches a container or a systemd manager.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREREQS = ROOT / "scripts" / "check-prereqs.sh"


def _bin(d: Path, docker_version: str) -> Path:
    b = d / "bin"
    b.mkdir()
    stubs = {
        "docker": ('if [ "$1" = --version ]; then echo "%s"; exit 0; fi\n'
                   "exit 0\n" % docker_version),
        "podman": "exit 0\n",
        "systemctl": f'echo "$*" >> "{d}/systemctl.log"\nexit 0\n',
        "loginctl": "exit 0\n",
    }
    for name, body in stubs.items():
        (b / name).write_text("#!/bin/sh\n" + body)
        (b / name).chmod(0o755)
    return b


def _detect(docker_version: str) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as d:
        b = _bin(Path(d), docker_version)
        env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")
        env.pop("PODMAN_COMPOSE_IN_POD", None)
        r = subprocess.run(
            ["bash", "-c", f'. "{PREREQS}"; prereq_detect_compose; '
             'echo "$ENGINE|${PODMAN_COMPOSE_IN_POD:-}"'],
            capture_output=True, text=True, env=env, check=True)
        engine, in_pod = r.stdout.strip().splitlines()[-1].split("|")
        return engine, in_pod


class DockerShimDetectionTests(unittest.TestCase):

    def test_the_podman_shim_is_podman(self):
        engine, in_pod = _detect("podman version 5.2.0")
        self.assertEqual(engine, "podman")
        self.assertEqual(in_pod, "false")

    def test_real_docker_stays_docker(self):
        engine, in_pod = _detect("Docker version 27.3.1, build ce12230")
        self.assertEqual(engine, "docker")
        self.assertEqual(in_pod, "")


class StartAtBootBehindTheShimTests(unittest.TestCase):

    def test_start_at_boot_writes_a_unit(self):
        if os.getuid() == 0:
            self.skipTest("as root the unit would go to /etc/systemd")
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            inst = d / "inst"
            (inst / "docker").mkdir(parents=True)
            (inst / "scripts").mkdir()
            shutil.copy(ROOT / "oikonome.sh", inst / "oikonome.sh")
            for f in ("check-prereqs.sh", "version-stamp.sh", "env-file.sh"):
                shutil.copy(ROOT / "scripts" / f, inst / "scripts" / f)
            (inst / "docker" / ".env").write_text(
                "COMPOSE_PROJECT_NAME=acme\n")
            b = _bin(d, "podman version 5.2.0")
            env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}",
                       HOME=str(d), XDG_CONFIG_HOME=str(d / "cfg"))
            r = subprocess.run(
                ["bash", str(inst / "oikonome.sh"), "start-at-boot"],
                capture_output=True, text=True, env=env, timeout=60,
                stdin=subprocess.DEVNULL)
            out = r.stdout + r.stderr
            self.assertEqual(r.returncode, 0, out)
            unit = d / "cfg" / "systemd" / "user" / "oikonome-acme.service"
            self.assertTrue(unit.exists(),
                            "start-at-boot claimed Docker restarts the stack "
                            "at boot, with no Docker daemon behind the CLI: "
                            + out)
            self.assertIn("enable oikonome-acme.service",
                          (d / "systemctl.log").read_text())


if __name__ == "__main__":
    unittest.main()
