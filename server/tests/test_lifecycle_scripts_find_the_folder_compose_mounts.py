"""The lifecycle scripts act on the database folder compose actually mounts.

compose reads OIKONOME_DATA_DIR from docker/.env as dotenv: quotes are
stripped, an unquoted value loses its trailing ` # comment`, `export ` is
accepted, and a relative value is resolved against docker/. A script that
reads the line literally acts on a different folder than postgres runs on:
a reset that "deletes" a path that does not exist and reports success with
the ledger still on disk, or an uninstall that decides a folder outside
the install is inside it and therefore releases its banks and throws away
the docker/.env (passwords + master key) the surviving database needs.
"""

import re
import unittest
from pathlib import Path

from . import lifecycle_stub as ls

ROOT = ls.ROOT


def _resolve(docker_dir: Path, default: str = "") -> str:
    r = ls.bash(f'. "{ROOT}/scripts/env-file.sh"; '
                f'data_dir_resolve "{docker_dir}" "{default}"',
                env=ls.run_env(Path("/nonexistent"), Path("/dev/null")))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class DataDirIsReadLikeComposeTests(unittest.TestCase):

    def _docker_dir(self, line: str) -> Path:
        t = ls.tempdir(self)
        d = t / "inst" / "docker"
        d.mkdir(parents=True)
        (d / ".env").write_text(f"A=1\n{line}\nB=2\n")
        return d

    def test_quotes_comments_and_export_are_read_as_compose_reads_them(self):
        for line, want in (
                ('OIKONOME_DATA_DIR="/srv/oik data"', "/srv/oik data"),
                ("OIKONOME_DATA_DIR='/srv/oik'", "/srv/oik"),
                ("OIKONOME_DATA_DIR=/mnt/big/oik  # big disk", "/mnt/big/oik"),
                ("export OIKONOME_DATA_DIR=/mnt/x", "/mnt/x")):
            with self.subTest(line=line):
                self.assertEqual(_resolve(self._docker_dir(line)), want)

    def test_a_relative_value_that_climbs_out_resolves_outside(self):
        d = self._docker_dir("OIKONOME_DATA_DIR=../../oikonome-data")
        got = _resolve(d)
        self.assertEqual(got, str((d.parent.parent / "oikonome-data")
                                  .resolve()))
        self.assertNotIn("..", got)

    def test_unset_falls_back_to_the_default(self):
        d = self._docker_dir("OTHER=1")
        self.assertEqual(_resolve(d, "../data/postgres"),
                         str((d.parent / "data" / "postgres").resolve()))
        self.assertEqual(_resolve(d, ""), "")


class UninstallJudgesInsideByTheRealPathTests(unittest.TestCase):
    """The inside/outside decision of uninstall.sh, run on its own: a
    folder reached by `../..` from docker/ is outside the install, and a
    folder that really is inside stays judged inside."""

    def _ext_data(self, value: str) -> str:
        src = (ROOT / "uninstall.sh").read_text()
        start = src.index('EXT_DATA=$(data_dir_resolve')
        end = src.index("\nfi\n", start) + 4
        t = ls.tempdir(self)
        here = t / "parent" / "inst"
        (here / "docker").mkdir(parents=True)
        (here / "docker" / ".env").write_text(f"OIKONOME_DATA_DIR={value}\n")
        r = ls.bash(f'set -euo pipefail; cd "{here}"; HERE=$(pwd); '
                    f'. "{ROOT}/scripts/env-file.sh"\n{src[start:end]}\n'
                    'printf "%s" "$EXT_DATA"',
                    env=ls.run_env(Path("/nonexistent"), Path("/dev/null")))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout, t

    def test_a_climbing_relative_path_is_kept_as_outside(self):
        got, t = self._ext_data("../../oikonome-data")
        self.assertEqual(got, str((t / "parent" / "oikonome-data").resolve()))

    def test_a_folder_inside_the_install_is_still_inside(self):
        got, _t = self._ext_data("../data/pg")
        self.assertEqual(got, "")


class ResetDeletesTheMountedFolderTests(unittest.TestCase):

    def test_a_quoted_data_dir_with_a_space_is_the_one_deleted(self):
        t = ls.tempdir(self)
        data = t / "big disk" / "oik"
        data.mkdir(parents=True)
        (data / "PG_VERSION").write_text("18\n")
        inst = ls.stage(t, "COMPOSE_PROJECT_NAME=acme\nOIKONOME_PORT=8999\n"
                           f'OIKONOME_DATA_DIR="{data}"  # on the big disk\n')
        binp = ls.make_bin(t)
        r = ls.bash(f'bash "{inst}/oikonome.sh" reset <<< reset',
                    env=ls.run_env(binp, t / "log"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(data.exists(), "reset left the mounted folder")
        self.assertTrue((inst / "install-ran").exists())

    def test_every_lifecycle_script_uses_the_shared_reader(self):
        """A literal `grep ^OIKONOME_DATA_DIR=` anywhere is the drift this
        file guards against coming back."""
        for name in ("oikonome.sh", "install.sh", "uninstall.sh",
                     "scripts/pg-major-migrate.sh"):
            src = (ROOT / name).read_text()
            with self.subTest(script=name):
                self.assertIsNone(re.search(r'grep "\^OIKONOME_DATA_DIR=', src))
                self.assertIn("data_dir_resolve", src)


if __name__ == "__main__":
    unittest.main()
