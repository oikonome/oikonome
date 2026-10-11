"""reset and uninstall must not strand the backups they promise to keep.

A dump restores the ledger on its own, but every bank connection, webhook
secret and two-factor seed in it is encrypted under the instance's master
key, which lives only in docker/.env. Both commands delete that file. So
reset carries the key into the reinstall unless a new one is asked for (and
then saves the old one next to the backups), and uninstall moves the key
out with the backups it moves out.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANAGER = ROOT / "oikonome.sh"
UNINSTALL = ROOT / "uninstall.sh"

KEY = "k" * 43 + "="


def _stub_engine(binp: Path) -> None:
    binp.mkdir()
    (binp / "docker").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        "exit 0\n")
    (binp / "docker").chmod(0o755)


def _env_file(path: Path) -> None:
    path.write_text("COMPOSE_PROJECT_NAME=acme\nOIKONOME_PORT=8999\n"
                    f"OIKONOME_MASTER_KEY={KEY}\nPOSTGRES_PASSWORD=pw\n")


class ResetKeepsTheMasterKeyTests(unittest.TestCase):

    def _stage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        inst = root / "install"
        inst.mkdir()
        shutil.copy(MANAGER, inst / "oikonome.sh")
        shutil.copytree(ROOT / "scripts", inst / "scripts")
        (inst / "docker").mkdir()
        _env_file(inst / "docker" / ".env")
        (inst / "data").mkdir()
        (inst / "backups").mkdir()
        (inst / "backups" / "oikonome-20260101-000000.sql.gz").write_bytes(
            b"x")
        seen = root / "install-saw.txt"
        # the reinstall is stubbed: it records only whether it was handed
        # the old key, never the key itself
        (inst / "install.sh").write_text(
            "#!/bin/sh\n"
            f'if [ "${{OIKONOME_KEEP_MASTER_KEY:-}}" = "{KEY}" ]; then '
            f'echo kept > "{seen}"; else echo new > "{seen}"; fi\n')
        (inst / "install.sh").chmod(0o755)
        binp = root / "bin"
        _stub_engine(binp)
        return inst, binp, seen

    def _run(self, *args, typed="reset"):
        inst, binp, seen = self._stage()
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}")
        env.pop("OIKONOME_ASSUME_YES", None)
        env.pop("OIKONOME_KEEP_MASTER_KEY", None)
        r = subprocess.run(["bash", str(inst / "oikonome.sh"), "reset", *args],
                           input=typed + "\n", capture_output=True,
                           text=True, env=env)
        return r, inst, seen

    def test_a_reset_reinstalls_under_the_same_key(self):
        r, _inst, seen = self._run()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(seen.read_text().strip(), "kept",
                         "the reinstall minted a new key, so the backups "
                         "the reset kept cannot decrypt their connections")
        self.assertIn("master key", r.stdout)

    def test_a_new_key_saves_the_old_one_with_the_backups(self):
        r, inst, seen = self._run("--new-key")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(seen.read_text().strip(), "new")
        saved = list((inst / "backups").glob("master-key-*.env"))
        self.assertEqual(len(saved), 1, r.stdout)
        self.assertIn(f"OIKONOME_MASTER_KEY={KEY}", saved[0].read_text())
        self.assertEqual(stat.S_IMODE(saved[0].stat().st_mode), 0o600)
        # said before the confirmation, not after the damage
        warn_at = r.stdout.find("cannot restore")
        self.assertNotEqual(warn_at, -1, r.stdout)
        self.assertLess(warn_at, r.stdout.find("Type 'reset'"))

    def test_an_aborted_reset_touches_nothing(self):
        r, inst, seen = self._run("--new-key", typed="no")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(seen.exists())
        self.assertTrue((inst / "docker" / ".env").exists())
        self.assertEqual(list((inst / "backups").glob("master-key-*")), [])


class FullResetReleasesBankLinksTests(unittest.TestCase):
    """The full reset deletes the database folder, which holds the only
    copy of every bank link's access token, so it releases them first."""

    def test_links_are_released_before_the_stack_goes_down(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        inst = root / "install"
        inst.mkdir()
        shutil.copy(MANAGER, inst / "oikonome.sh")
        shutil.copytree(ROOT / "scripts", inst / "scripts")
        (inst / "docker").mkdir()
        _env_file(inst / "docker" / ".env")
        (inst / "install.sh").write_text("#!/bin/sh\nexit 0\n")
        (inst / "install.sh").chmod(0o755)
        log = root / "engine.log"
        binp = root / "bin"
        binp.mkdir()
        (binp / "docker").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
            'if [ "$1" = "ps" ]; then echo app_1; exit 0; fi\n'
            f'echo "$*" >> "{log}"\nexit 0\n')
        (binp / "docker").chmod(0o755)
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}")
        env.pop("OIKONOME_ASSUME_YES", None)
        r = subprocess.run(["bash", str(inst / "oikonome.sh"), "reset"],
                           input="reset\n", capture_output=True, text=True,
                           env=env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        calls = log.read_text().splitlines()
        rel = [i for i, c in enumerate(calls) if "--release-links" in c]
        down = [i for i, c in enumerate(calls) if " down" in f" {c}"]
        self.assertTrue(rel, "the reset deleted the database without "
                             "releasing its bank links")
        self.assertTrue(down)
        self.assertLess(rel[0], down[0])


class UninstallMovesTheKeyWithTheBackupsTests(unittest.TestCase):

    def _run(self, keep: str, env_text: str | None = None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        home = root / "home"
        home.mkdir()
        inst = root / "install"
        inst.mkdir()
        shutil.copy(UNINSTALL, inst / "uninstall.sh")
        (inst / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "env-file.sh",
                    inst / "scripts" / "env-file.sh")
        (inst / "docker").mkdir()
        if env_text is None:
            _env_file(inst / "docker" / ".env")
        else:
            (inst / "docker" / ".env").write_text(env_text)
        (inst / "backups").mkdir()
        (inst / "backups" / "oikonome-20260101-000000.sql.gz").write_bytes(
            b"x")
        binp = root / "bin"
        _stub_engine(binp)
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}",
                   HOME=str(home))
        env.pop("OIKONOME_ASSUME_YES", None)
        # setsid: no controlling terminal, so the prompts read the piped
        # answers instead of a real tty
        r = subprocess.run(["setsid", "-w", "bash", str(inst / "uninstall.sh")],
                           input=f"acme\n{keep}\n", capture_output=True,
                           text=True, env=env)
        return r, home, inst

    def test_kept_backups_take_the_master_key_with_them(self):
        r, home, inst = self._run("")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(inst.exists())
        safe = list(home.glob("oikonome-backups-*"))
        self.assertEqual(len(safe), 1, r.stdout)
        keys = list(safe[0].glob("master-key*.env"))
        self.assertEqual(len(keys), 1,
                         "the backups moved out without the key they need")
        self.assertIn(f"OIKONOME_MASTER_KEY={KEY}", keys[0].read_text())
        self.assertEqual(stat.S_IMODE(keys[0].stat().st_mode), 0o600)

    def _saved_key_lines(self, env_text: str) -> list[str]:
        r, home, _inst = self._run("", env_text)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        safe = list(home.glob("oikonome-backups-*"))
        self.assertEqual(len(safe), 1, r.stdout)
        keys = list(safe[0].glob("master-key*.env"))
        self.assertEqual(len(keys), 1,
                         "the backups moved out without the key they need")
        return [ln for ln in keys[0].read_text().splitlines()
                if ln and not ln.startswith("#")]

    def test_an_export_prefixed_key_still_goes_with_the_backups(self):
        # compose accepts `export KEY=`; the key it gave the stack is the
        # one the backups need
        lines = self._saved_key_lines(
            "COMPOSE_PROJECT_NAME=acme\nOIKONOME_PORT=8999\n"
            f"export OIKONOME_MASTER_KEY={KEY}\nPOSTGRES_PASSWORD=pw\n")
        self.assertEqual(lines, [f"OIKONOME_MASTER_KEY={KEY}"])

    def test_a_key_set_twice_saves_only_the_one_compose_used(self):
        # compose takes the last assignment; the saved file must hold that
        # key alone, not the stale one followed by a stray line
        old = "o" * 43 + "="
        lines = self._saved_key_lines(
            "COMPOSE_PROJECT_NAME=acme\nOIKONOME_PORT=8999\n"
            f"OIKONOME_MASTER_KEY={old}\nOIKONOME_MASTER_KEY={KEY}\n"
            "POSTGRES_PASSWORD=pw\n")
        self.assertEqual(lines, [f"OIKONOME_MASTER_KEY={KEY}"])

    def test_declined_backups_leave_no_key_behind(self):
        r, home, _inst = self._run("n")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(list(home.glob("oikonome-backups-*")), [])


class UninstallClearsARootOwnedOpsStateTests(unittest.TestCase):
    """A host-watcher installer runs as root and could leave a directory
    under ops-state/ that the owner cannot empty. Uninstall clears it the
    way it clears the database folder, through a throwaway root container,
    and names what it could not remove instead of only saying it failed."""

    def _run(self, container_clears: bool):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        home = root / "home"
        home.mkdir()
        inst = root / "install"
        inst.mkdir()
        shutil.copy(UNINSTALL, inst / "uninstall.sh")
        (inst / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "env-file.sh",
                    inst / "scripts" / "env-file.sh")
        (inst / "docker").mkdir()
        _env_file(inst / "docker" / ".env")
        stuck = inst / "ops-state" / "watchers"
        stuck.mkdir(parents=True)
        (stuck / "command").write_text("x")
        # a directory the caller cannot write stands in for a root-owned one
        stuck.chmod(0o555)

        def unlock():
            if stuck.exists():
                stuck.chmod(0o755)
            tmp.cleanup()
        self.addCleanup(unlock)
        binp = root / "bin"
        binp.mkdir()
        # the root container: given -v <dir>:/d, it empties <dir>
        clear = ('d=$(printf "%s\\n" "$@" | sed -n "s/:\\/d$//p")\n'
                 'chmod -R u+w "$d"; find "$d" -mindepth 1 -delete\n'
                 if container_clears else "")
        (binp / "docker").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
            'if [ "$1" = "run" ]; then\n' + clear + "exit 0\nfi\n"
            "exit 0\n")
        (binp / "docker").chmod(0o755)
        # neither a host sudo nor a user namespace (where the caller is
        # root over its own files) may finish the job behind the test
        for tool in ("sudo", "podman"):
            (binp / tool).write_text("#!/bin/sh\nexit 1\n")
            (binp / tool).chmod(0o755)
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}",
                   HOME=str(home))
        env.pop("OIKONOME_ASSUME_YES", None)
        r = subprocess.run(["setsid", "-w", "bash", str(inst / "uninstall.sh")],
                           input="acme\nn\n", capture_output=True,
                           text=True, env=env)
        return r, inst

    def test_a_root_owned_watchers_directory_does_not_block_removal(self):
        r, inst = self._run(container_clears=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(inst.exists())

    def test_what_is_left_behind_is_named(self):
        r, inst = self._run(container_clears=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(inst.exists())
        self.assertIn("ops-state/watchers", r.stdout)


class InstallHonoursAKeptKeyTests(unittest.TestCase):

    def test_the_fresh_install_path_reads_the_kept_key(self):
        src = (ROOT / "install.sh").read_text()
        self.assertTrue("OIKONOME_KEEP_MASTER_KEY" in src,
                        "install.sh mints a new key even when reset hands "
                        "it the old one")



def _engine(binp: Path, log: Path, *, app_up: bool = False,
            run_rc: int = 0) -> None:
    """A docker stand-in that logs every call. `ps` names an app container
    when app_up; `compose run` (the one-off migrate container that reaches
    an external database) exits run_rc."""
    binp.mkdir(exist_ok=True)
    (binp / "docker").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        f'echo "$*" >> "{log}"\n'
        + ('if [ "$1" = "ps" ]; then echo app_1; exit 0; fi\n' if app_up
           else "")
        + f'if [ "$1" = "compose" ] && [ "$2" = "run" ]; then exit {run_rc}; fi\n'
        "exit 0\n")
    (binp / "docker").chmod(0o755)


EXTERNAL = ("COMPOSE_FILE=compose.yaml:compose.external-db.yaml\n"
            "OIKONOME_DB_HOST=db.example.test\n"
            "OIKONOME_DB_NAME=ledger\n"
            "POSTGRES_PASSWORD=provider-admin-pw\n")


class ResetCarriesTheInstanceTests(unittest.TestCase):
    """A reset wipes the data and the generated secrets, not the way the
    instance is set up: the operator's settings come back, the kept key is
    checked and saved before anything is deleted, and on an external
    database the database itself is what gets wiped."""

    def _run(self, env_text: str, *args, typed="reset", app_up=False,
             run_rc=0):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        inst = root / "install"
        inst.mkdir()
        shutil.copy(MANAGER, inst / "oikonome.sh")
        shutil.copytree(ROOT / "scripts", inst / "scripts")
        (inst / "docker").mkdir()
        (inst / "docker" / ".env").write_bytes(env_text.encode())
        (inst / "data").mkdir()
        (inst / "data" / "PG_VERSION").write_text("18\n")
        (inst / "backups").mkdir()
        (inst / "backups" / "oikonome-20260101-000000.sql.gz").write_bytes(
            b"x")
        out = root / "seen"
        out.mkdir()
        # the reinstall stub records what it was handed (synthetic values)
        (inst / "install.sh").write_text(
            "#!/bin/sh\n"
            f'printf "%s" "${{OIKONOME_KEEP_MASTER_KEY:-}}" > "{out}/key"\n'
            f'printf "%s\\n" "${{OIKONOME_RESET_SETTINGS:-}}" > "{out}/settings"\n')
        (inst / "install.sh").chmod(0o755)
        log = root / "engine.log"
        log.write_text("")
        _engine(root / "bin", log, app_up=app_up, run_rc=run_rc)
        env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}")
        for k in ("OIKONOME_ASSUME_YES", "OIKONOME_KEEP_MASTER_KEY",
                  "OIKONOME_RESET_SETTINGS"):
            env.pop(k, None)
        r = subprocess.run(["bash", str(inst / "oikonome.sh"), "reset", *args],
                           input=typed + "\n", capture_output=True,
                           text=True, env=env)
        return r, inst, out, log.read_text()

    def _settings(self, out: Path) -> dict:
        lines = (out / "settings").read_text().splitlines()
        return dict(ln.split("=", 1) for ln in lines if "=" in ln)

    def test_operator_settings_survive_and_generated_secrets_do_not(self):
        env = ("COMPOSE_PROJECT_NAME=acme\nOIKONOME_PORT=8999\n"
               f"OIKONOME_MASTER_KEY={KEY}\nPOSTGRES_PASSWORD=pw\n"
               "OIKONOME_APP_PASSWORD=apw\nOIKONOME_ADMIN_PASSWORD=adpw\n"
               "REDIS_PASSWORD=rpw\nOIKONOME_SETUP_TOKEN=tok\n"
               "OIKONOME_VERSION=1.0.0\n"
               "# a comment\n"
               "COMPOSE_PROFILES=tls,llm\nOIKONOME_DOMAIN=money.example.test\n"
               "OIKONOME_BASE_URL=https://money.example.test\n"
               "OIKONOME_BIND_ADDRESS=127.0.0.1\nOIKONOME_ADMIN_ENABLED=1\n"
               "OIKONOME_SMTP_HOST=smtp.example.test\n"
               "OIKONOME_LLM_MODEL=qwen2.5:1.5b\n")
        r, _inst, out, _log = self._run(env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        got = self._settings(out)
        for k, v in (("COMPOSE_PROFILES", "tls,llm"),
                     ("OIKONOME_DOMAIN", "money.example.test"),
                     ("OIKONOME_BASE_URL", "https://money.example.test"),
                     ("OIKONOME_BIND_ADDRESS", "127.0.0.1"),
                     ("OIKONOME_ADMIN_ENABLED", "1"),
                     ("OIKONOME_SMTP_HOST", "smtp.example.test"),
                     ("OIKONOME_LLM_MODEL", "qwen2.5:1.5b"),
                     ("COMPOSE_PROJECT_NAME", "acme")):
            self.assertEqual(got.get(k), v, f"{k} was dropped by the reset")
        for k in ("POSTGRES_PASSWORD", "OIKONOME_APP_PASSWORD",
                  "OIKONOME_ADMIN_PASSWORD", "REDIS_PASSWORD",
                  "OIKONOME_SETUP_TOKEN", "OIKONOME_MASTER_KEY",
                  "OIKONOME_VERSION"):
            self.assertNotIn(k, got, f"{k} should be minted fresh")
        prompt = r.stdout.find("Type 'reset'")
        kept = r.stdout.find("Kept:")
        self.assertNotEqual(kept, -1, r.stdout)
        self.assertLess(kept, prompt)
        self.assertIn("bind address", r.stdout[:prompt])

    def test_a_quoted_crlf_key_is_carried_and_saved_exactly(self):
        env = (f'COMPOSE_PROJECT_NAME=acme\r\nOIKONOME_MASTER_KEY="{KEY}"\r\n'
               "POSTGRES_PASSWORD=pw\r\n")
        r, inst, out, _log = self._run(env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual((out / "key").read_text(), KEY)
        saved = list((inst / "backups").glob("master-key-*.env"))
        self.assertEqual(len(saved), 1, r.stdout)
        self.assertIn(f"OIKONOME_MASTER_KEY={KEY}\n", saved[0].read_text())
        self.assertEqual(stat.S_IMODE(saved[0].stat().st_mode), 0o600)

    def test_a_kept_key_is_saved_beside_the_backups_too(self):
        r, inst, _out, _log = self._run(
            f"COMPOSE_PROJECT_NAME=acme\nOIKONOME_MASTER_KEY={KEY}\n")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        saved = list((inst / "backups").glob("master-key-*.env"))
        self.assertEqual(len(saved), 1,
                         "the only copy of the key went to the reinstall")
        self.assertIn(f"OIKONOME_MASTER_KEY={KEY}", saved[0].read_text())

    def test_a_value_that_is_not_a_key_stops_the_reset_untouched(self):
        r, inst, out, log = self._run(
            "COMPOSE_PROJECT_NAME=acme\nOIKONOME_MASTER_KEY=not-a-key\n",
            app_up=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((inst / "docker" / ".env").exists())
        self.assertTrue((inst / "data" / "PG_VERSION").exists())
        self.assertFalse((out / "key").exists(), "the reinstall ran")
        self.assertNotIn("--release-links", log)
        self.assertNotIn("down", log)
        self.assertNotIn("Type 'reset'", r.stdout)

    def test_the_message_names_what_the_key_still_protects(self):
        r, _inst, _out, _log = self._run(
            f"COMPOSE_PROJECT_NAME=acme\nOIKONOME_MASTER_KEY={KEY}\n")
        before = r.stdout[:r.stdout.find("Type 'reset'")]
        self.assertIn("two-factor", before)
        self.assertIn("linked again", before)
        self.assertNotIn("restore their bank connections", r.stdout)

    def test_an_external_database_is_dropped_and_its_connection_kept(self):
        r, inst, out, log = self._run(
            f"COMPOSE_PROJECT_NAME=acme\nOIKONOME_MASTER_KEY={KEY}\n"
            + EXTERNAL)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        drops = [ln for ln in log.splitlines()
                 if "compose run" in ln and "migrate" in ln and "DROP" in ln]
        self.assertTrue(drops, "the managed database was never dropped:\n"
                               + log)
        self.assertIn("ledger", drops[0])
        self.assertNotIn("./data", r.stdout)
        self.assertIn("db.example.test", r.stdout)
        got = self._settings(out)
        self.assertEqual(got.get("COMPOSE_FILE"),
                         "compose.yaml:compose.external-db.yaml")
        self.assertEqual(got.get("OIKONOME_DB_HOST"), "db.example.test")
        self.assertEqual(got.get("POSTGRES_PASSWORD"), "provider-admin-pw",
                         "the provider's admin password cannot be minted")

    def test_a_failed_external_drop_deletes_nothing_else(self):
        r, inst, out, _log = self._run(
            f"COMPOSE_PROJECT_NAME=acme\nOIKONOME_MASTER_KEY={KEY}\n"
            + EXTERNAL, run_rc=1)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((inst / "docker" / ".env").exists())
        self.assertFalse((out / "key").exists())
        self.assertNotIn("removed", r.stdout)


class UninstallKeepsWhatOutlivesItTests(unittest.TestCase):

    def _run(self, env_text, typed, *, app_up=False, backups=False,
             locked=False, ext_data=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        home = root / "home"
        home.mkdir()
        inst = root / "install"
        inst.mkdir()
        shutil.copy(UNINSTALL, inst / "uninstall.sh")
        (inst / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "env-file.sh",
                    inst / "scripts" / "env-file.sh")
        (inst / "docker").mkdir()
        if ext_data:
            (root / "extdata").mkdir()
            env_text += f"OIKONOME_DATA_DIR={root / 'extdata'}\n"
        if env_text is not None:
            (inst / "docker" / ".env").write_text(env_text)
        if backups:
            (inst / "backups").mkdir()
            (inst / "backups" / "oikonome-20260101-000000.sql.gz"
             ).write_bytes(b"x")
        if locked:
            # a directory this user cannot empty: the removal ladder stops
            (inst / "stuck").mkdir()
            (inst / "stuck" / "f").write_text("x")
            (inst / "stuck").chmod(0o555)
            self.addCleanup(lambda: (inst / "stuck").exists()
                            and (inst / "stuck").chmod(0o755))
        log = root / "engine.log"
        log.write_text("")
        binp = root / "bin"
        _engine(binp, log, app_up=app_up)
        for tool in ("podman", "sudo"):
            (binp / tool).write_text("#!/bin/sh\nexit 1\n")
            (binp / tool).chmod(0o755)
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}",
                   HOME=str(home))
        env.pop("OIKONOME_ASSUME_YES", None)
        r = subprocess.run(["setsid", "-w", "bash", str(inst / "uninstall.sh")],
                           input=typed, capture_output=True, text=True,
                           env=env)
        return r, root, home, inst, log.read_text()

    def test_an_env_without_a_project_name_still_completes(self):
        for backups in (True, False):
            with self.subTest(backups=backups):
                r, _root, home, inst, _log = self._run(
                    f"OIKONOME_MASTER_KEY={KEY}\n", "install\n\n",
                    backups=backups)
                self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
                self.assertFalse(inst.exists())
                if backups:
                    self.assertEqual(len(list(home.glob("oikonome-backups-*"))),
                                     1)

    def test_banks_are_released_before_the_stack_goes_down(self):
        r, _root, _home, _inst, log = self._run(
            "COMPOSE_PROJECT_NAME=acme\n", "acme\n", app_up=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        calls = log.splitlines()
        rel = [i for i, c in enumerate(calls) if "--release-links" in c]
        down = [i for i, c in enumerate(calls) if "compose down" in c]
        self.assertTrue(rel, "the uninstall deleted the only copy of the "
                             "bank tokens without releasing them")
        self.assertLess(rel[0], down[0])

    def test_a_stopped_app_is_named_for_the_provider_dashboard(self):
        r, *_ = self._run("COMPOSE_PROJECT_NAME=acme\n", "acme\n")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("provider's dashboard", r.stdout)

    def test_an_external_database_is_kept_with_its_env_and_said_so(self):
        r, _root, home, inst, log = self._run(
            f"COMPOSE_PROJECT_NAME=acme\nOIKONOME_MASTER_KEY={KEY}\n"
            + EXTERNAL, "acme\n", app_up=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(inst.exists())
        kept = list(home.glob("*.docker.env"))
        self.assertEqual(len(kept), 1, r.stdout)
        self.assertIn("POSTGRES_PASSWORD=provider-admin-pw",
                      kept[0].read_text())
        self.assertIn(f"OIKONOME_MASTER_KEY={KEY}", kept[0].read_text())
        self.assertEqual(stat.S_IMODE(kept[0].stat().st_mode), 0o600)
        self.assertNotIn("DATABASE, and everything below", r.stdout)
        self.assertIn("NOT deleted", r.stdout)
        # the database outlives the uninstall, so its links stay usable
        self.assertNotIn("--release-links", log)

    @unittest.skipIf(os.geteuid() == 0, "root can empty any directory")
    def test_a_kept_data_folder_gets_its_env_before_the_removal(self):
        r, root, _home, _inst, _log = self._run(
            f"COMPOSE_PROJECT_NAME=acme\nOIKONOME_MASTER_KEY={KEY}\n",
            "acme\n", locked=True, ext_data=True)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("Couldn't remove", r.stdout)
        kept = root / "extdata.docker.env"
        self.assertTrue(kept.exists(), "the removal stopped early and the "
                                       "kept database lost its passwords")
        self.assertIn(f"OIKONOME_MASTER_KEY={KEY}", kept.read_text())


def _stage_installer(root: Path, *, info_rc=0, ps_rc=0, env_text=None):
    """install.sh with the prereq check stubbed and the Postgres major
    migration made to fail, so a run stops right after configuration —
    before any build, start or health wait."""
    inst = root / "install"
    (inst / "docker").mkdir(parents=True)
    (inst / "scripts").mkdir()
    shutil.copy(ROOT / "install.sh", inst / "install.sh")
    shutil.copy(ROOT / "scripts" / "version-stamp.sh",
                inst / "scripts" / "version-stamp.sh")
    shutil.copy(ROOT / "scripts" / "env-file.sh",
                inst / "scripts" / "env-file.sh")
    shutil.copy(ROOT / "docker" / ".env.example",
                inst / "docker" / ".env.example")
    (inst / "scripts" / "check-prereqs.sh").write_text(
        'require_prereqs() { COMPOSE="docker compose"; ENGINE=docker; }\n'
        "port_busy() { return 1; }\n")
    (inst / "scripts" / "gen-version-history.sh").write_text(
        "#!/bin/sh\nexit 0\n")
    (inst / "scripts" / "pg-major-migrate.sh").write_text(
        "#!/bin/sh\necho STOP-HERE; exit 1\n")
    for f in ("gen-version-history.sh", "pg-major-migrate.sh"):
        (inst / "scripts" / f).chmod(0o755)
    if env_text is not None:
        (inst / "docker" / ".env").write_text(env_text)
    binp = root / "bin"
    binp.mkdir()
    (binp / "docker").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = info ]; then exit {info_rc}; fi\n'
        f'if [ "$1" = ps ]; then exit {ps_rc}; fi\n'
        "exit 0\n")
    (binp / "docker").chmod(0o755)
    return inst, binp


class InstallerTests(unittest.TestCase):

    def _run(self, inst, binp, extra_env=None):
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}")
        for k in ("OIKONOME_KEEP_MASTER_KEY", "OIKONOME_RESET_SETTINGS",
                  "OIKONOME_PORT", "OIKONOME_ASSUME_YES"):
            env.pop(k, None)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(inst / "install.sh")], capture_output=True,
            text=True, env=env, stdin=subprocess.DEVNULL, timeout=60,
            preexec_fn=lambda: os.umask(0o022))

    def test_an_unreachable_engine_stops_the_upgrade_out_loud(self):
        with tempfile.TemporaryDirectory() as d:
            inst, binp = _stage_installer(
                Path(d), info_rc=1,
                env_text=f"OIKONOME_MASTER_KEY={KEY}\n")
            r = self._run(inst, binp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("Can't reach", r.stdout + r.stderr)

    def test_a_failing_container_probe_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            inst, binp = _stage_installer(
                Path(d), ps_rc=1, env_text=f"OIKONOME_MASTER_KEY={KEY}\n")
            r = self._run(inst, binp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ps failed", r.stdout + r.stderr)
            self.assertNotIn("STOP-HERE", r.stdout)

    def test_the_env_temp_file_is_never_readable_by_others(self):
        with tempfile.TemporaryDirectory() as d:
            inst, binp = _stage_installer(
                Path(d), env_text=f"OIKONOME_MASTER_KEY={KEY}\n")
            modes = Path(d) / "modes"
            real_mv = shutil.which("mv")
            # records the mode of every file renamed over docker/.env
            (binp / "mv").write_text(
                "#!/bin/sh\n"
                f'stat -c %a "$1" >> "{modes}"\n'
                f'exec "{real_mv}" "$@"\n')
            (binp / "mv").chmod(0o755)
            r = self._run(inst, binp)
            self.assertIn("STOP-HERE", r.stdout, r.stdout + r.stderr)
            seen = modes.read_text().split()
            self.assertTrue(seen)
            self.assertEqual(set(seen), {"600"},
                             "set_env's temp copy of .env was readable")
            self.assertEqual(
                [p.name for p in (inst / "docker").glob(".env.*")
                 if p.name != ".env.example"], [])

    def test_a_reset_reinstall_keeps_the_carried_settings(self):
        with tempfile.TemporaryDirectory() as d:
            inst, binp = _stage_installer(Path(d))
            r = self._run(inst, binp, {
                "OIKONOME_PORT": "8999",
                "OIKONOME_KEEP_MASTER_KEY": KEY,
                "OIKONOME_RESET_SETTINGS":
                    "COMPOSE_PROFILES=tls\nOIKONOME_BIND_ADDRESS=127.0.0.1\n"
                    "COMPOSE_FILE=compose.yaml:compose.external-db.yaml\n"
                    "POSTGRES_PASSWORD=provider-admin-pw\n"
                    "COMPOSE_PROJECT_NAME=acme\n"})
            self.assertIn("STOP-HERE", r.stdout, r.stdout + r.stderr)
            lines = (inst / "docker" / ".env").read_text().splitlines()
            got = {}
            for ln in lines:
                if "=" in ln and not ln.startswith("#"):
                    k, v = ln.split("=", 1)
                    self.assertNotIn(k, got, f"{k} written twice")
                    got[k] = v
            self.assertEqual(got["COMPOSE_PROFILES"], "tls")
            self.assertEqual(got["OIKONOME_BIND_ADDRESS"], "127.0.0.1")
            self.assertEqual(got["COMPOSE_FILE"],
                             "compose.yaml:compose.external-db.yaml")
            self.assertEqual(got["POSTGRES_PASSWORD"], "provider-admin-pw")
            self.assertEqual(got["COMPOSE_PROJECT_NAME"], "acme")
            self.assertEqual(got["OIKONOME_MASTER_KEY"], KEY)
            self.assertNotIn(got["OIKONOME_APP_PASSWORD"],
                             ("", "change-me-too"))
            self.assertTrue(got["OIKONOME_SETUP_TOKEN"])

    def test_a_handed_over_value_that_is_not_a_key_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            inst, binp = _stage_installer(Path(d))
            r = self._run(inst, binp, {"OIKONOME_KEEP_MASTER_KEY": "abc"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("not a master key", r.stdout)
            self.assertFalse((inst / "docker" / ".env").exists())


if __name__ == "__main__":
    unittest.main()
