"""A backup that isn't one must never reach a restore, and a wipe must
name what it is wiping.

Three failures share one root: a tool that reports success because nothing
errored, rather than because something is true.

  * `pg_dump | gzip` aborts the moment the database goes away (a concurrent
    restore/reset/major-migration stops the container), and gzip reports
    success on a stream that stopped after zero bytes. The half-written file
    lands with the newest timestamp, right where the restore picker looks.
  * `psql -v ON_ERROR_STOP=1` exits 0 on an empty statement stream — there
    is nothing to error on — so loading that file "succeeds" over a database
    that was just dropped, and the tool prints "Restore complete" over an
    empty instance.
  * `reset-data` deletes every tenant behind one generic confirm word, so
    the same keystrokes clear a test household on a laptop and every
    customer on a multi-tenant host.

These shell the real scripts out against scratch directories with a stub
`docker` in front of them (precedent: test_installer_env_flag_zero_is_off.py,
test_backup_dump_permissions.py), and drive the reset CLI against the real
database.
"""

import gzip
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from oikonome.db import reset, tenancy

from .util import _admin_dsn, TEST_DB, TODAY, add_txn, make_db, write_config

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "backup.sh"
MANAGER = ROOT / "oikonome.sh"

# A stub pg_dump whose behaviour is chosen by $STUB_MODE. The payload is
# base64 of random bytes: a compressible payload would slip under the
# script's 1KB floor and prove nothing.
_STUB_DOCKER = r"""#!/bin/sh
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "exec" ]; then
  case "$STUB_MODE" in
    ok)        echo "CREATE TABLE transactions (id text);"
               head -c 40000 /dev/urandom | base64; exit 0;;
    midfail)   echo "CREATE TABLE transactions (id text);"
               head -c 40000 /dev/urandom | base64; exit 1;;
    instafail) echo "pg_dump: error: connection to server was lost" >&2; exit 1;;
    emptyok)   exit 0;;
  esac
fi
exit 1
"""


def _install(root: Path) -> Path:
    (root / "install" / "docker").mkdir(parents=True)
    (root / "install" / "docker" / ".env").write_text("OIKONOME_X=1\n")
    binp = root / "bin"
    binp.mkdir()
    (binp / "docker").write_text(_STUB_DOCKER)
    (binp / "docker").chmod(0o755)
    return binp


class BackupPublishesOnlyWholeDumpsTests(unittest.TestCase):
    """scripts/backup.sh: the picker must never see a file the restore
    cannot use."""

    def _run(self, mode: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        binp = _install(root)
        dest = root / "backups"
        env = dict(os.environ, STUB_MODE=mode,
                   PATH=f"{binp}:{os.environ['PATH']}")
        r = subprocess.run(["bash", str(BACKUP), str(root / "install"),
                            str(dest)],
                           capture_output=True, text=True, env=env)
        return r, dest

    def _visible_dumps(self, dest: Path):
        """Exactly what the restore picker globs (oikonome.sh: `ls -1t
        $dest/oikonome-*.sql.gz`)."""
        return sorted(dest.glob("oikonome-*.sql.gz"))

    def test_a_whole_dump_is_published(self):
        r, dest = self._run("ok")
        self.assertEqual(r.returncode, 0, r.stderr)
        dumps = self._visible_dumps(dest)
        self.assertEqual(len(dumps), 1)
        with gzip.open(dumps[0], "rb") as fh:      # raises on a bad stream
            self.assertIn(b"CREATE TABLE", fh.read(4096))

    def test_a_dump_cut_off_mid_stream_is_not_published(self):
        # pg_dump dies partway through: gzip still exits 0 and the bytes
        # written so far are a valid gzip file. Publishing it would put a
        # half-database under the newest timestamp.
        r, dest = self._run("midfail")
        self.assertNotEqual(r.returncode, 0,
                            "a failed pg_dump reported a successful backup")
        self.assertEqual(self._visible_dumps(dest), [],
                         "a partial dump was left where the restore picker "
                         "lists it")

    def test_a_dump_that_never_started_is_not_published(self):
        # The container is already gone: pg_dump writes nothing and fails.
        r, dest = self._run("instafail")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self._visible_dumps(dest), [])

    def test_an_empty_but_valid_gzip_is_not_published(self):
        # The exact artifact that restored as a silent success: pg_dump
        # exits 0 having streamed nothing, gzip writes a ~20-byte file that
        # decompresses cleanly to nothing.
        r, dest = self._run("emptyok")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self._visible_dumps(dest), [])

    def test_no_partial_file_is_left_behind_at_all(self):
        for mode in ("midfail", "instafail", "emptyok"):
            with self.subTest(mode=mode):
                _, dest = self._run(mode)
                leftovers = [p.name for p in dest.iterdir()] if dest.exists() else []
                self.assertEqual(leftovers, [],
                                 "a failed backup left a file behind")


class RestoreRefusesUnrestorableDumpsTests(unittest.TestCase):
    """oikonome.sh's screening of a dump BEFORE it drops anything. The
    function is exercised directly: everything around it needs a live
    stack, and the decision to refuse is the whole point."""

    def _verdict(self, blob: bytes):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Path(tmp.name) / "oikonome-20260101-000000.sql.gz"
        f.write_bytes(blob)
        script = (f'eval "$(sed -n \'/^dump_looks_real/,/^}}/p\' '
                  f'"{MANAGER}")"\n'
                  f'if dump_looks_real "{f}"; then echo ACCEPT; '
                  f'else echo "REJECT: $REJECT"; fi')
        r = subprocess.run(["bash", "-c", script],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def _dump(self, body: bytes) -> bytes:
        return gzip.compress(body)

    def test_a_real_dump_is_accepted(self):
        body = (b"--\n-- PostgreSQL database dump\n--\nSET statement_timeout = 0;\n"
                b"CREATE TABLE public.transactions (id uuid);\n"
                + b"INSERT INTO public.transactions VALUES ('x');\n" * 100)
        self.assertEqual(self._verdict(self._dump(body)), "ACCEPT")

    def test_an_empty_dump_is_refused(self):
        # 20 bytes, perfectly valid gzip, decompresses to nothing — psql
        # would load it without a single error.
        self.assertIn("REJECT", self._verdict(self._dump(b"")))

    def test_a_header_only_dump_is_refused(self):
        # pg_dump got as far as its SET preamble and the connection died:
        # over 1KB of real SQL, not one table.
        body = b"--\n-- PostgreSQL database dump\n--\n" + b"-- filler\n" * 200
        self.assertIn("REJECT", self._verdict(self._dump(body)))

    def test_a_corrupt_file_is_refused(self):
        self.assertIn("REJECT", self._verdict(b"this is not gzip at all"))

    def test_a_truncated_gzip_is_refused(self):
        # The file the picker sees when a backup was killed while gzip was
        # still writing: real content, no gzip trailer.
        body = (b"CREATE TABLE public.transactions (id uuid);\n"
                + b"INSERT INTO public.transactions VALUES ('x');\n" * 500)
        blob = self._dump(body)
        self.assertIn("REJECT", self._verdict(blob[:len(blob) // 2]))


class ResetDataNamesWhatItDestroysTests(unittest.TestCase):
    """`reset-data` is the one command that deletes financial data while
    keeping the logins that make the instance look intact afterwards."""

    def _tid(self, conn):
        return str(conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def _seed(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_txn(conn, TODAY, 50.0, "GROCERY MART", account="chk")
        return conn

    def test_summary_counts_without_deleting(self):
        conn = self._seed()
        tid = self._tid(conn)
        before = reset.count_tenant_data(tid)
        self.assertGreater(sum(before.values()), 0)
        text = reset.summary([(tid, "owner@example.dev")])
        self.assertIn(tid, text)
        self.assertIn("owner@example.dev", text)
        self.assertIn(str(sum(before.values())), text)
        # the point of a summary: everything it counted is still there
        self.assertEqual(reset.count_tenant_data(tid), before)

    def test_summary_mode_of_the_cli_deletes_nothing(self):
        conn = self._seed()
        tid = self._tid(conn)
        before = reset.count_tenant_data(tid)
        self.assertEqual(reset.main(["reset", "--summary"]), 0)
        self.assertEqual(reset.count_tenant_data(tid), before)

    def test_wiping_every_tenant_must_be_asked_for(self):
        # Two tenants = not a single household, so a bare invocation must
        # not default to "all of them".
        a, b = self._seed(), self._seed()
        tid_a, tid_b = self._tid(a), self._tid(b)
        rc = reset.main(["reset"])
        self.assertEqual(rc, 2, "a bare reset wiped every tenant on the "
                                "instance without being asked")
        self.assertGreater(sum(reset.count_tenant_data(tid_a).values()), 0)
        self.assertGreater(sum(reset.count_tenant_data(tid_b).values()), 0)

    def test_a_tenant_id_scopes_the_wipe_to_that_tenant(self):
        a, b = self._seed(), self._seed()
        tid_a, tid_b = self._tid(a), self._tid(b)
        self.assertEqual(reset.main(["reset", tid_a]), 0)
        self.assertEqual(sum(reset.count_tenant_data(tid_a).values()), 0)
        self.assertGreater(sum(reset.count_tenant_data(tid_b).values()), 0)

    def test_all_tenants_lists_the_owner_email(self):
        conn = self._seed()
        tid = self._tid(conn)
        email = f"owner-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'owner')", (tid, email))
        finally:
            admin.close()
        listed = dict(reset.all_tenants())
        self.assertEqual(listed.get(tid), email,
                         "a confirmation that shows only UUIDs tells an "
                         "operator nothing about whose data this is")


class ResetDataConfirmationTests(unittest.TestCase):
    """The typed confirmation must name the instance, and a mismatch must
    destroy nothing — the bar `uninstall` already sets."""

    def _stage(self, iname: str = "acme-hosted"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        inst = root / "install"
        inst.mkdir()
        shutil.copy(MANAGER, inst / "oikonome.sh")
        shutil.copytree(ROOT / "scripts", inst / "scripts")
        (inst / "docker").mkdir()
        (inst / "docker" / ".env").write_text(
            f"COMPOSE_PROJECT_NAME={iname}\n")
        binp = root / "bin"
        binp.mkdir()
        ran = root / "ran.log"
        # `docker ps` names the app container; `docker exec … --summary`
        # prints an inventory; anything else is recorded so the test can
        # see whether a wipe was actually attempted.
        (binp / "docker").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
            'if [ "$1" = "ps" ]; then echo app_1; exit 0; fi\n'
            'if [ "$1" = "exec" ]; then\n'
            '  case "$*" in\n'
            '    *--summary*) echo "2 tenant(s), 4210 rows of financial data";'
            ' echo "  ada@example.dev  (t-1)  3000 rows in 12 tables";'
            ' echo "  bob@example.dev  (t-2)  1210 rows in 9 tables"; exit 0;;\n'
            f'    *) echo "WIPE $*" >> "{ran}"; exit 0;;\n'
            "  esac\n"
            "fi\nexit 0\n")
        (binp / "docker").chmod(0o755)
        return inst, binp, ran

    def _run(self, typed: str, iname: str = "acme-hosted"):
        inst, binp, ran = self._stage(iname)
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}")
        env.pop("OIKONOME_ASSUME_YES", None)
        r = subprocess.run(["bash", str(inst / "oikonome.sh"), "reset-data"],
                           input=typed + "\n", capture_output=True,
                           text=True, env=env)
        wiped = ran.read_text() if ran.exists() else ""
        return r, wiped

    def test_the_prompt_names_the_instance_and_the_tenant_count(self):
        r, _ = self._run("acme-hosted")
        self.assertIn("acme-hosted", r.stdout)
        self.assertIn("2 tenant(s), 4210 rows", r.stdout,
                      "the confirmation does not say how much, or whose, "
                      "data is about to be destroyed")
        self.assertIn("ada@example.dev", r.stdout)

    def test_the_generic_word_no_longer_confirms_anything(self):
        r, wiped = self._run("reset-data")
        self.assertEqual(wiped, "",
                         "a generic confirm word wiped every tenant")
        self.assertNotEqual(r.returncode, 0)

    def test_the_wrong_instance_name_destroys_nothing(self):
        r, wiped = self._run("some-other-instance")
        self.assertEqual(wiped, "")
        self.assertNotEqual(r.returncode, 0)

    def test_the_instance_name_confirms(self):
        r, wiped = self._run("acme-hosted")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--all", wiped,
                      "the confirmed wipe did not run")


if __name__ == "__main__":
    unittest.main()
