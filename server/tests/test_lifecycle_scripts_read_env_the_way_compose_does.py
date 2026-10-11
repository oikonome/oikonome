"""The lifecycle scripts read docker/.env the way compose reads it.

Compose accepts `export KEY=value`, strips the quotes of `KEY="value"` and,
when a key is assigned twice, uses the last assignment. A literal
`grep ^KEY= | cut` read does none of that, so the script and the stack it
manages can disagree about a setting:

- install.sh reads the master key and the database passwords on every
  upgrade. Reading `export OIKONOME_MASTER_KEY=…` as "absent" either stops
  an upgrade of a running instance with a false "missing" message, or (stack
  stopped) appends a freshly minted key that compose then prefers, after
  which nothing stored under the old key decrypts again.
- oikonome.sh hands COMPOSE_PROJECT_NAME to status, backup, restore, reset
  and uninstall; read as empty (or as two lines) they look for the wrong
  project's containers and volumes.
- scripts/backup.sh, the nightly dump, picks its dump path from
  COMPOSE_FILE. Missing an exported external-database setting, it dumps
  through a bundled postgres container that does not exist, and fails
  every night.

A value written back by either script replaces every spelling of its key,
so one assignment is left and there is nothing for the two readers to
disagree about.
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from . import lifecycle_stub as ls
from .test_reset_and_uninstall_keep_the_master_key import KEY, _stage_installer

ROOT = Path(__file__).resolve().parents[2]
OTHER_KEY = "m" * 43 + "="


def _assignments(path: Path, key: str) -> list[str]:
    pat = re.compile(rf"^\s*(export\s+)?{key}\s*=")
    return [ln for ln in path.read_text().splitlines() if pat.match(ln)]


def _compose_value(path: Path, key: str) -> str | None:
    """KEY as compose reads it: last assignment, `export` and quotes off.
    Written here independently of the scripts under test."""
    val = None
    for ln in _assignments(path, key):
        v = ln.split("=", 1)[1].strip()
        if v[:1] in ("'", '"'):
            v = v[1:v.index(v[0], 1)]
        val = v
    return val


def _oikonome_funcs(*names: str) -> str:
    """The named helpers lifted out of oikonome.sh, after the shared .env
    readers they are built on."""
    src = (ROOT / "oikonome.sh").read_text()
    out = [f'. "{ROOT}/scripts/env-file.sh"\n']
    for n in names:
        m = re.search(rf"^{n}\(\) \{{\n.*?^\}}\n", src, re.M | re.S)
        assert m, f"oikonome.sh lost its {n} helper"
        out.append(m.group(0))
    return "".join(out)


class InstallerReadsTheEnvLikeComposeTests(unittest.TestCase):

    def _run(self, inst, binp):
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}")
        for k in ("OIKONOME_KEEP_MASTER_KEY", "OIKONOME_RESET_SETTINGS",
                  "OIKONOME_PORT", "OIKONOME_ASSUME_YES"):
            env.pop(k, None)
        return subprocess.run(
            ["bash", str(inst / "install.sh")], capture_output=True,
            text=True, env=env, stdin=subprocess.DEVNULL, timeout=60,
            preexec_fn=lambda: os.umask(0o022))

    @staticmethod
    def _running(binp: Path) -> None:
        # `ps` names this instance's app container: the stack is up
        (binp / "docker").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = ps ]; then echo acme-app-1; exit 0; fi\n'
            "exit 0\n")

    def test_an_exported_master_key_is_kept_when_the_stack_is_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            inst, binp = _stage_installer(Path(d), env_text=(
                "COMPOSE_PROJECT_NAME=acme\n"
                f'export OIKONOME_MASTER_KEY="{KEY}"\n'
                "export REDIS_PASSWORD='queue-pw'\n"
                "export OIKONOME_ADMIN_PASSWORD=admin-pw\n"))
            r = self._run(inst, binp)
            self.assertIn("STOP-HERE", r.stdout, r.stdout + r.stderr)
            envf = inst / "docker" / ".env"
            self.assertEqual(_compose_value(envf, "OIKONOME_MASTER_KEY"), KEY)
            self.assertEqual(_compose_value(envf, "REDIS_PASSWORD"), "queue-pw")
            self.assertEqual(_compose_value(envf, "OIKONOME_ADMIN_PASSWORD"),
                             "admin-pw")
            self.assertNotIn("generated one", r.stdout)
            self.assertEqual(len(_assignments(envf, "OIKONOME_MASTER_KEY")), 1)

    def test_exported_secrets_satisfy_the_running_instance_guard(self):
        with tempfile.TemporaryDirectory() as d:
            inst, binp = _stage_installer(Path(d), env_text=(
                "COMPOSE_PROJECT_NAME=acme\n"
                f"export OIKONOME_MASTER_KEY={KEY}\n"
                "export OIKONOME_APP_PASSWORD=app-pw\n"
                'export POSTGRES_PASSWORD="pg-pw"\n'
                "export OIKONOME_ADMIN_PASSWORD=admin-pw\n"
                "export REDIS_PASSWORD=queue-pw\n"))
            self._running(binp)
            r = self._run(inst, binp)
            self.assertNotIn("is missing", r.stdout + r.stderr)
            self.assertIn("STOP-HERE", r.stdout, r.stdout + r.stderr)
            self.assertEqual(_compose_value(inst / "docker" / ".env",
                                            "OIKONOME_MASTER_KEY"), KEY)

    def test_a_secret_blanked_by_a_later_line_stops_a_running_upgrade(self):
        # compose takes the last assignment, so the stack would get no
        # admin password: the guard must see what compose sees
        with tempfile.TemporaryDirectory() as d:
            inst, binp = _stage_installer(Path(d), env_text=(
                "COMPOSE_PROJECT_NAME=acme\n"
                f"OIKONOME_MASTER_KEY={KEY}\n"
                "OIKONOME_APP_PASSWORD=app-pw\n"
                "POSTGRES_PASSWORD=pg-pw\n"
                "OIKONOME_ADMIN_PASSWORD=admin-pw\n"
                "REDIS_PASSWORD=queue-pw\n"
                "OIKONOME_ADMIN_PASSWORD=\n"))
            self._running(binp)
            r = self._run(inst, binp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("missing OIKONOME_ADMIN_PASSWORD", r.stdout)
            self.assertNotIn("STOP-HERE", r.stdout)

    def test_the_last_master_key_is_the_one_kept(self):
        with tempfile.TemporaryDirectory() as d:
            inst, binp = _stage_installer(Path(d), env_text=(
                f"OIKONOME_MASTER_KEY={OTHER_KEY}\n"
                f"export OIKONOME_MASTER_KEY={KEY}\n"))
            r = self._run(inst, binp)
            self.assertIn("STOP-HERE", r.stdout, r.stdout + r.stderr)
            self.assertEqual(_compose_value(inst / "docker" / ".env",
                                            "OIKONOME_MASTER_KEY"), KEY)

    def test_a_value_written_back_replaces_every_spelling_of_its_key(self):
        with tempfile.TemporaryDirectory() as d:
            inst, binp = _stage_installer(Path(d), env_text=(
                f"OIKONOME_MASTER_KEY={KEY}\n"
                "export OIKONOME_VERSION=v0.0.1\n"
                "  OIKONOME_VERSION = v0.0.2\n"))
            r = self._run(inst, binp)
            self.assertIn("STOP-HERE", r.stdout, r.stdout + r.stderr)
            self.assertEqual(
                len(_assignments(inst / "docker" / ".env",
                                 "OIKONOME_VERSION")), 1)


class ManagerReadsTheEnvLikeComposeTests(unittest.TestCase):

    def _env_get(self, env_text: str, key: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "docker").mkdir()
            (Path(d) / "docker" / ".env").write_text(env_text)
            r = subprocess.run(
                ["bash", "-c",
                 f'ROOT="$1"; {_oikonome_funcs("env_value", "env_get")}\n'
                 'env_get "$2"',
                 "_", d, key], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.rstrip("\n")

    def test_an_exported_quoted_project_name_is_read(self):
        self.assertEqual(
            self._env_get('export COMPOSE_PROJECT_NAME="acme"\n',
                          "COMPOSE_PROJECT_NAME"), "acme")

    def test_a_project_name_set_twice_resolves_to_the_last(self):
        self.assertEqual(
            self._env_get("COMPOSE_PROJECT_NAME=old\n"
                          "COMPOSE_PROJECT_NAME=acme\n",
                          "COMPOSE_PROJECT_NAME"), "acme")

    def test_set_env_leaves_one_assignment(self):
        with tempfile.TemporaryDirectory() as d:
            envf = Path(d) / "docker" / ".env"
            envf.parent.mkdir()
            envf.write_text("export COMPOSE_PROFILES=tls\n"
                            "COMPOSE_PROFILES = tls\nOTHER=1\n")
            r = subprocess.run(
                ["bash", "-c",
                 f'ROOT="$1"; {_oikonome_funcs("set_env")}\n'
                 "set_env COMPOSE_PROFILES tls,llm",
                 "_", d], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(_assignments(envf, "COMPOSE_PROFILES"),
                             ["COMPOSE_PROFILES=tls,llm"])
            self.assertIn("OTHER=1", envf.read_text())

    def test_status_looks_for_the_exported_project(self):
        t = ls.tempdir(self)
        inst = ls.stage(t, 'export COMPOSE_PROJECT_NAME="acme"\n')
        binp = ls.make_bin(t)
        log = t / "log"
        r = ls.bash(f'bash "{inst}/oikonome.sh" status </dev/null',
                    env=ls.run_env(binp, log))
        calls = log.read_text() if log.exists() else ""
        self.assertIn("label=com.docker.compose.project=acme", calls,
                      r.stdout + r.stderr)


# A stand-in engine for the nightly dump: it logs every call and plays
# pg_dump on either path the script can take, so the log alone says which
# database the dump was taken from. Real enough for the script's own
# checks: schema in the stream, and more than 1KB after gzip.
_DUMP_ENGINE = """#!/bin/sh
echo "$*" >> "$OIK_LOG"
if [ "$1" = compose ] && [ "$2" = version ]; then exit 0; fi
if [ "$1" = compose ] && { [ "$2" = exec ] || [ "$2" = run ]; }; then
  echo "CREATE TABLE transactions (id text);"
  head -c 40000 /dev/urandom | base64
  exit 0
fi
exit 1
"""


class NightlyBackupReadsTheEnvLikeComposeTests(unittest.TestCase):

    def _dump_path(self, env_text: str, compose_file: str | None = None) -> str:
        """Which database the nightly dump was read from: 'external' (the
        migrate service's client against the provider DSN) or 'bundled'
        (the postgres container)."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "install" / "docker").mkdir(parents=True)
            (root / "install" / "docker" / ".env").write_text(env_text)
            binp = root / "bin"
            binp.mkdir()
            (binp / "docker").write_text(_DUMP_ENGINE)
            (binp / "docker").chmod(0o755)
            log = root / "log"
            env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}",
                       OIK_LOG=str(log))
            # compose prefers the environment's COMPOSE_FILE; the test
            # reads the .env alone
            env.pop("COMPOSE_FILE", None)
            if compose_file is not None:
                env["COMPOSE_FILE"] = compose_file
            r = subprocess.run(
                ["bash", str(ROOT / "scripts" / "backup.sh"),
                 str(root / "install"), str(root / "backups")],
                capture_output=True, text=True, env=env,
                stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            calls = log.read_text()
        if "run --rm --no-deps -T migrate" in calls:
            return "external"
        if "exec -T postgres pg_dump" in calls:
            return "bundled"
        return calls

    def test_an_exported_external_database_is_dumped_from_the_provider(self):
        self.assertEqual(self._dump_path(
            "export COMPOSE_FILE=compose.yaml:compose.external-db.yaml\n"),
            "external")

    def test_a_spaced_quoted_external_database_is_dumped_from_the_provider(self):
        self.assertEqual(self._dump_path(
            'COMPOSE_FILE = "compose.yaml:compose.external-db.yaml"\n'),
            "external")

    def test_an_external_database_unset_by_a_later_line_dumps_the_bundled_one(self):
        self.assertEqual(self._dump_path(
            "COMPOSE_FILE=compose.yaml:compose.external-db.yaml\n"
            "COMPOSE_FILE=compose.yaml\n"), "bundled")

    def test_a_commented_out_external_database_dumps_the_bundled_one(self):
        self.assertEqual(self._dump_path(
            "# COMPOSE_FILE=compose.yaml:compose.external-db.yaml\n"),
            "bundled")

    def test_the_environment_wins_over_the_file_as_it_does_for_compose(self):
        self.assertEqual(self._dump_path(
            "COMPOSE_FILE=compose.yaml\n",
            compose_file="compose.yaml:compose.external-db.yaml"), "external")

    def test_the_bundled_database_is_dumped_by_default(self):
        self.assertEqual(self._dump_path("OIKONOME_X=1\n"), "bundled")


if __name__ == "__main__":
    unittest.main()
